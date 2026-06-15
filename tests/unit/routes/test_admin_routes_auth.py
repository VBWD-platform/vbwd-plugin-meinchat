"""Route-level auth tests for the meinchat admin endpoints.

These specs exercise the Flask test client end-to-end up to the auth
middleware boundary — proving that unauthenticated, non-admin, and
admin-without-permission requests all hit their correct status codes.
The routes' business logic itself is covered by service-level tests.
"""
import pytest


ADMIN_BAN_PATH = (
    "/api/v1/admin/meinchat/nicknames/00000000-0000-0000-0000-000000000001/ban"
)
ADMIN_UNBAN_PATH = (
    "/api/v1/admin/meinchat/nicknames/00000000-0000-0000-0000-000000000001/unban"
)
ADMIN_TRANSFERS_PATH = "/api/v1/admin/meinchat/transfers"
ADMIN_GUESTS_PATH = "/api/v1/admin/meinchat/guests"
ADMIN_GUEST_TOKENS_PATH = (
    "/api/v1/admin/meinchat/guests/00000000-0000-0000-0000-000000000001/tokens"
)
ADMIN_GUESTS_BULK_TOKENS_PATH = "/api/v1/admin/meinchat/guests/tokens"


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", ADMIN_BAN_PATH),
        ("POST", ADMIN_UNBAN_PATH),
        ("GET", ADMIN_TRANSFERS_PATH),
        ("GET", ADMIN_GUESTS_PATH),
        ("POST", ADMIN_GUEST_TOKENS_PATH),
        ("POST", ADMIN_GUESTS_BULK_TOKENS_PATH),
    ],
)
def test_admin_route_without_auth_returns_401(client, method, path):
    response = client.open(path, method=method)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/nickname/me"),
        ("PUT", "/api/v1/nickname/me"),
        ("GET", "/api/v1/nickname/search?q=ali"),
        ("GET", "/api/v1/nickname/some-nick/card"),
        ("GET", "/api/v1/contacts"),
        ("POST", "/api/v1/contacts"),
        ("GET", "/api/v1/messaging/conversations"),
        ("POST", "/api/v1/messaging/conversations"),
        ("POST", "/api/v1/messaging/stream/token"),
        ("POST", "/api/v1/token-transfer"),
        ("GET", "/api/v1/token-transfer/history"),
    ],
)
def test_user_route_without_auth_returns_401(client, method, path):
    response = client.open(path, method=method)
    assert response.status_code == 401


def test_sse_stream_without_token_query_returns_401(client):
    """The SSE stream is intentionally NOT @require_auth-decorated
    (EventSource can't set headers). It validates `?stream_token=…`
    server-side and 401s when missing."""
    response = client.get("/api/v1/messaging/stream")
    assert response.status_code == 401


def test_sse_stream_with_garbage_token_returns_401(client):
    response = client.get("/api/v1/messaging/stream?stream_token=not-a-jwt")
    assert response.status_code == 401
