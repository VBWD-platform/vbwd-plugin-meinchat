"""S28.3a §6.4 — GET /api/v1/messaging/capabilities[?me=true]."""
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from plugins.meinchat.meinchat.extensibility import registry
from plugins.meinchat.meinchat.extensibility.lifecycle import (
    IConversationCapabilities,
    PlainCapability,
)

_PATH = "/api/v1/messaging/capabilities"


class _Caps:
    def __init__(self, protocols):
        self._protocols = set(protocols)

    def for_conversation(self, conv):
        return set(self._protocols)


@contextmanager
def _auth(app, caller_user_id: UUID):
    fake_caller = MagicMock()
    fake_caller.id = caller_user_id
    fake_caller.is_admin = False
    fake_caller.status.value = "ACTIVE"
    with ExitStack() as stack:
        repo = stack.enter_context(patch("vbwd.middleware.auth.UserRepository"))
        auth = stack.enter_context(patch("vbwd.middleware.auth.AuthService"))
        enabled = MagicMock()
        enabled.status = "enabled"
        stack.enter_context(
            patch.object(app.config_store, "get_by_name", return_value=enabled)
        )
        repo.return_value.find_by_id.return_value = fake_caller
        auth.return_value.verify_token.return_value = str(caller_user_id)
        yield


def _get(client, query=""):
    return client.get(_PATH + query, headers={"Authorization": "Bearer test-token"})


class TestCapabilitiesEndpoint:
    def test_default_registry_returns_plain(self, app, client):
        # Pin the meinchat-default capability registry deterministically — an
        # enabled meinchat-plus instance also registers E2eV1Capability at boot,
        # so this spec sets its own known state instead of relying on global
        # boot order (mirrors the other specs in this class).
        registry.reset_for_tests(IConversationCapabilities)
        registry.register(IConversationCapabilities, PlainCapability())
        try:
            with _auth(app, uuid4()):
                response = _get(client)
        finally:
            registry.reset_for_tests(IConversationCapabilities)
            registry.register(IConversationCapabilities, PlainCapability())
        assert response.status_code == 200
        assert response.get_json() == {"server": ["plain"]}

    def test_added_capability_impl_is_included(self, app, client):
        registry.register(IConversationCapabilities, _Caps({"foo"}))
        try:
            with _auth(app, uuid4()):
                response = _get(client)
        finally:
            registry.reset_for_tests(IConversationCapabilities)
            # Restore the default the app registered at boot.
            from plugins.meinchat.meinchat.extensibility.lifecycle import (
                PlainCapability,
            )

            registry.register(IConversationCapabilities, PlainCapability())
        body = response.get_json()
        assert "foo" in body["server"]
        assert "plain" in body["server"]

    def test_me_excludes_e2e_without_device_key(self, app, client):
        registry.register(IConversationCapabilities, _Caps({"plain", "e2e_v1"}))
        try:
            with _auth(app, uuid4()):
                response = _get(client, "?me=true")
        finally:
            registry.reset_for_tests(IConversationCapabilities)
            from plugins.meinchat.meinchat.extensibility.lifecycle import (
                PlainCapability,
            )

            registry.register(IConversationCapabilities, PlainCapability())
        body = response.get_json()
        assert "e2e_v1" in body["server"]
        # Caller has no device key (NullDeviceDirectory) → not usable.
        assert body["me"] == ["plain"]

    def test_auth_required(self, client):
        assert client.get(_PATH).status_code == 401
