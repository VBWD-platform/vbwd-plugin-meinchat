"""Route-level tests for `POST /api/v1/messaging/conversations` rate limiting.

Covers S26 §4.3 + §4.4:
- Opening an EXISTING conversation never burns a `new_conversation` slot
  (today's prod screenshot repro negation).
- Brand-new conversations consume slots; web baseline is 10/h, iOS override
  is 60/h via the `X-Client-Platform` header.
- Headers: `X-Conversation-Existed: true` on the existing-chat path;
  `X-Rate-Limit-Category` + `Retry-After` on 429.
- The bucket is shared per (user_id, category) across platforms; only the
  ceiling differs per request.
- token_transfer's use of `start_or_get` is unaffected.

These specs let the meinchat rate-limiter run for real (InMemoryCounterBackend
fallback when Redis isn't reachable) and stub out everything else: auth, the
nickname repository, the conversation service, the conversation serializer.
Each test uses a fresh `user_id` so the per-user counter starts at zero.
"""
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4


_NEW_CONVERSATION_LIMIT_WEB = 10
_NEW_CONVERSATION_LIMIT_IOS = 60


def _test_config():
    return {
        "rate_new_conversation_per_window": _NEW_CONVERSATION_LIMIT_WEB,
        "rate_new_conversation_window_seconds": 3600,
        "rate_nickname_search_per_window": 30,
        "rate_nickname_search_window_seconds": 60,
        "rate_message_send_per_window": 30,
        "rate_message_send_window_seconds": 60,
        "rate_attachment_send_per_window": 6,
        "rate_attachment_send_window_seconds": 3600,
        "rate_ios_new_conversation_per_window": _NEW_CONVERSATION_LIMIT_IOS,
        "rate_ios_new_conversation_window_seconds": 3600,
    }


class _PeerRegistry:
    """Maps nickname → fake nickname row. Tests register peers up-front,
    then POST with the matching `peer_nickname`."""

    def __init__(self):
        self._peers = {}

    def add(self, nickname: str) -> MagicMock:
        peer_user_id = uuid4()
        fake_nickname_row = MagicMock()
        fake_nickname_row.user_id = peer_user_id
        fake_nickname_row.banned = False
        fake_nickname_row.search_hidden = False
        fake_nickname_row.nickname = nickname.lower()
        self._peers[nickname.lower()] = fake_nickname_row
        return fake_nickname_row

    def resolve(self, nickname: str):
        return self._peers.get(nickname.strip().lower())


class _StubConversationService:
    """In-memory stand-in for ConversationService. Tracks how many distinct
    pairs were created so tests can assert on it."""

    def __init__(self):
        self._existing_by_pair = {}
        self.start_or_get_call_count = 0
        self.find_between_call_count = 0

    def seed_existing(self, user_a, user_b):
        key = tuple(sorted([str(user_a), str(user_b)]))
        fake_conv = MagicMock()
        fake_conv.id = uuid4()
        fake_conv.participant_low_id = user_a
        fake_conv.participant_high_id = user_b
        self._existing_by_pair[key] = fake_conv
        return fake_conv

    def find_between(self, user_a, user_b):
        self.find_between_call_count += 1
        key = tuple(sorted([str(user_a), str(user_b)]))
        return self._existing_by_pair.get(key)

    def start_or_get(self, user_a, user_b):
        self.start_or_get_call_count += 1
        key = tuple(sorted([str(user_a), str(user_b)]))
        if key not in self._existing_by_pair:
            self.seed_existing(user_a, user_b)
        return self._existing_by_pair[key]


@contextmanager
def _route_harness(
    app,
    caller_user_id: UUID,
    peers: _PeerRegistry,
    conversation_service: _StubConversationService,
):
    """Patch the auth + plugin-config + repo seams so the route runs against
    in-memory fakes. The meinchat rate-limiter is left untouched — counters
    are real, keyed on the (caller_user_id, category) we supply."""
    fake_caller = MagicMock()
    fake_caller.id = caller_user_id
    fake_caller.is_admin = False
    fake_caller.status.value = "ACTIVE"

    def _fake_nickname_repo_instance(*_args, **_kwargs):
        instance = MagicMock()
        instance.find_by_nickname_ci.side_effect = peers.resolve

        def _find_by_user_id(_uid):
            stub = MagicMock()
            stub.nickname = "stub-peer"
            return stub

        instance.find_by_user_id.side_effect = _find_by_user_id
        return instance

    def _fake_serialize(conv, _user_id):
        return {"id": str(getattr(conv, "id", "stub"))}

    with ExitStack() as stack:
        patch_user_repo = stack.enter_context(
            patch("vbwd.middleware.auth.UserRepository")
        )
        patch_auth_service = stack.enter_context(
            patch("vbwd.middleware.auth.AuthService")
        )
        stack.enter_context(
            patch(
                "plugins.meinchat.meinchat.routes.NicknameRepository",
                side_effect=_fake_nickname_repo_instance,
            )
        )
        stack.enter_context(
            patch(
                "plugins.meinchat.meinchat.routes._conversation_service",
                return_value=conversation_service,
            )
        )
        stack.enter_context(
            patch(
                "plugins.meinchat.meinchat.routes._meinchat_config",
                return_value=_test_config(),
            )
        )
        stack.enter_context(
            patch(
                "plugins.meinchat.meinchat.routes._serialize_conversation_for_user",
                side_effect=_fake_serialize,
            )
        )

        patch_user_repo.return_value.find_by_id.return_value = fake_caller
        patch_auth_service.return_value.verify_token.return_value = str(caller_user_id)
        # Pin the meinchat in-memory rate-limit backend so we never hit
        # whatever Redis state lives on a dev machine, and so the counter
        # starts clean even if a prior test happened to use the same uuid.
        from plugins.meinchat.meinchat.services.rate_limiter import (
            InMemoryCounterBackend,
            RateLimiter,
        )

        original_rate_limiter = getattr(app, "_meinchat_rate_limiter", None)
        app._meinchat_rate_limiter = RateLimiter(InMemoryCounterBackend())
        try:
            yield
        finally:
            if original_rate_limiter is None:
                if hasattr(app, "_meinchat_rate_limiter"):
                    delattr(app, "_meinchat_rate_limiter")
            else:
                app._meinchat_rate_limiter = original_rate_limiter


def _post_start(client, peer_nickname: str, headers: dict | None = None):
    return client.post(
        "/api/v1/messaging/conversations",
        json={"peer_nickname": peer_nickname},
        headers={"Authorization": "Bearer test-token", **(headers or {})},
    )


class TestStartConversationRateLimit:
    def test_repeat_open_same_peer_never_429(self, app, client):
        # Today's prod repro negation: opening @lololo 100× burns 0 slots.
        caller = uuid4()
        peers = _PeerRegistry()
        peer = peers.add("@lololo")
        conv_service = _StubConversationService()
        conv_service.seed_existing(caller, peer.user_id)

        with _route_harness(app, caller, peers, conv_service):
            for _ in range(100):
                response = _post_start(client, "@lololo")
                assert response.status_code == 200
                assert response.headers.get("X-Conversation-Existed") == "true"
        assert conv_service.start_or_get_call_count == 0

    def test_first_creates_to_10_distinct_peers_then_429(self, app, client):
        caller = uuid4()
        peers = _PeerRegistry()
        for index in range(11):
            peers.add(f"peer{index}")
        conv_service = _StubConversationService()

        with _route_harness(app, caller, peers, conv_service):
            for index in range(_NEW_CONVERSATION_LIMIT_WEB):
                response = _post_start(client, f"peer{index}")
                assert response.status_code == 200, (
                    f"peer{index} expected 200, got {response.status_code} "
                    f"body={response.get_data(as_text=True)}"
                )
            blocked = _post_start(client, f"peer{_NEW_CONVERSATION_LIMIT_WEB}")
            assert blocked.status_code == 429
            assert blocked.headers["Retry-After"]
            assert blocked.headers["X-Rate-Limit-Category"] == "new_conversation"

    def test_mixed_existing_and_new_only_counts_new(self, app, client):
        caller = uuid4()
        peers = _PeerRegistry()
        for index in range(11):
            peers.add(f"peer{index}")
        conv_service = _StubConversationService()

        with _route_harness(app, caller, peers, conv_service):
            # 9 brand-new creates → 9 slots used
            for index in range(9):
                assert _post_start(client, f"peer{index}").status_code == 200

            # 50× reopening those 9 → 0 slots used
            for _round in range(50):
                for index in range(9):
                    response = _post_start(client, f"peer{index}")
                    assert response.status_code == 200
                    assert response.headers.get("X-Conversation-Existed") == "true"

            # 10th distinct peer → slot 10 used
            assert _post_start(client, "peer9").status_code == 200

            # 11th distinct peer → 429
            blocked = _post_start(client, "peer10")
            assert blocked.status_code == 429
            assert blocked.headers["X-Rate-Limit-Category"] == "new_conversation"

    def test_existing_response_header(self, app, client):
        caller = uuid4()
        peers = _PeerRegistry()
        peers.add("alice")
        conv_service = _StubConversationService()

        with _route_harness(app, caller, peers, conv_service):
            first = _post_start(client, "alice")
            assert first.status_code == 200
            # First call was a create, so the header is absent.
            assert "X-Conversation-Existed" not in first.headers

            second = _post_start(client, "alice")
            assert second.status_code == 200
            assert second.headers["X-Conversation-Existed"] == "true"

    def test_ios_header_lifts_new_conversation_ceiling_to_60(self, app, client):
        caller = uuid4()
        peers = _PeerRegistry()
        for index in range(_NEW_CONVERSATION_LIMIT_IOS + 1):
            peers.add(f"peer{index}")
        conv_service = _StubConversationService()
        headers = {"X-Client-Platform": "ios"}

        with _route_harness(app, caller, peers, conv_service):
            for index in range(_NEW_CONVERSATION_LIMIT_IOS):
                response = _post_start(client, f"peer{index}", headers=headers)
                assert (
                    response.status_code == 200
                ), f"peer{index} expected 200, got {response.status_code}"
            blocked = _post_start(
                client, f"peer{_NEW_CONVERSATION_LIMIT_IOS}", headers=headers
            )
            assert blocked.status_code == 429
            assert blocked.headers["X-Rate-Limit-Category"] == "new_conversation"

    def test_platform_header_case_insensitive(self, app, client):
        # iOS as "iOS" must behave the same as "ios".
        caller = uuid4()
        peers = _PeerRegistry()
        for index in range(_NEW_CONVERSATION_LIMIT_WEB + 5):
            peers.add(f"peer{index}")
        conv_service = _StubConversationService()
        headers = {"X-Client-Platform": "iOS"}

        with _route_harness(app, caller, peers, conv_service):
            # Beyond the web ceiling — only the iOS ceiling can allow this.
            for index in range(_NEW_CONVERSATION_LIMIT_WEB + 5):
                response = _post_start(client, f"peer{index}", headers=headers)
                assert response.status_code == 200

    def test_shared_bucket_per_user_across_platforms(self, app, client):
        # 5 web + 6 iOS new peers → 11 slots used, still under iOS ceiling 60.
        # Then keep pushing on web — the 16th web request must hit web's
        # ceiling of 10 (shared bucket, per-request ceiling).
        caller = uuid4()
        peers = _PeerRegistry()
        for index in range(20):
            peers.add(f"peer{index}")
        conv_service = _StubConversationService()

        with _route_harness(app, caller, peers, conv_service):
            for index in range(5):
                assert _post_start(client, f"peer{index}").status_code == 200

            for index in range(5, 11):
                response = _post_start(
                    client,
                    f"peer{index}",
                    headers={"X-Client-Platform": "ios"},
                )
                assert response.status_code == 200

            # 5 more web POSTs (counter now at 11..15) — all over web ceiling
            # of 10 → 429 from the first one onward.
            for index in range(11, 16):
                response = _post_start(client, f"peer{index}")
                assert response.status_code == 429
                assert response.headers["X-Rate-Limit-Category"] == "new_conversation"

            # Same user from iOS to a fresh peer → counter is at 16, still
            # under the iOS ceiling of 60 → 200.
            ios_response = _post_start(
                client,
                "peer16",
                headers={"X-Client-Platform": "ios"},
            )
            assert ios_response.status_code == 200


class TestTokenTransferRegression:
    """ConversationService.start_or_get's contract is unchanged — it still
    returns a Conversation (not a tuple). Token-transfer's auto-create path
    is unaffected by the route reorder."""

    def test_start_or_get_return_type_is_conversation(self):
        from unittest.mock import MagicMock
        from uuid import uuid4

        from plugins.meinchat.meinchat.models.conversation import Conversation
        from plugins.meinchat.meinchat.services.conversation_service import (
            ConversationService,
        )

        repo = MagicMock()
        repo.find_by_pair.return_value = None
        repo.save.side_effect = lambda row: row
        service = ConversationService(repo=repo)

        result = service.start_or_get(uuid4(), uuid4())
        assert isinstance(result, Conversation)
        # Not a tuple — token_transfer_service relies on this.
        assert not isinstance(result, tuple)


class TestMeinchatRateLimitTelemetry:
    """S33 — the meinchat custom limiter (`_enforce_rate`) emits one WARN-level
    structured log line on every 429 so "users hit the cap" reports are
    answerable with grep instead of a screenshot."""

    def test_meinchat_429_emits_warn_log_with_category_and_user(self, app, caplog):
        import logging

        from flask import g

        from plugins.meinchat.meinchat import routes as meinchat_routes
        from plugins.meinchat.meinchat.services.rate_limiter import RateLimitExceeded

        user_id = uuid4()
        fake_limiter = MagicMock()
        fake_limiter.check.side_effect = RateLimitExceeded(
            "message_send", retry_after_seconds=42
        )

        with app.test_request_context("/", headers={"X-Client-Platform": "web"}):
            g.user_id = user_id
            with patch.object(
                meinchat_routes, "_rate_limiter", return_value=fake_limiter
            ), patch.object(
                meinchat_routes, "_meinchat_config", return_value=_test_config()
            ):
                with caplog.at_level(logging.WARNING):
                    response = meinchat_routes._enforce_rate("message_send")

        assert response is not None
        assert response.status_code == 429
        assert "429" in caplog.text, caplog.text
        assert "message_send" in caplog.text
        assert str(user_id) in caplog.text
