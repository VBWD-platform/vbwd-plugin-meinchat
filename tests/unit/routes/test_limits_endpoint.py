"""Route-level tests for `GET /api/v1/messaging/limits` (S28.0 §2.3).

The endpoint surfaces the four operator-tunable retention/size knobs so
clients can poll them on cold start. It carries operator knobs ONLY —
the capability surface (`enabled_protocols`) deliberately lives on
`/messaging/capabilities` from S28.3a (one endpoint, one concern; DRY).

These specs run the Flask test client end-to-end through the auth
middleware boundary. Auth is stubbed; the meinchat config is supplied via
a patched `_meinchat_config` so a test can flip a value and assert the
next call reflects it.
"""
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest


_LIMITS_PATH = "/api/v1/messaging/limits"

_FOUR_KEYS = {
    "messages_retention_days_server",
    "messages_retention_days_client_suggested",
    "attachments_retention_days_server",
    "ciphertext_max_bytes",
}


def _limits_config(**overrides):
    """A config dict carrying the four operator knobs at their documented
    defaults, with optional per-test overrides."""
    config = {
        "messages_retention_days_server": 2,
        "messages_retention_days_client_suggested": 10,
        "attachments_retention_days_server": 2,
        "ciphertext_max_bytes": 16384,
    }
    config.update(overrides)
    return config


@contextmanager
def _enabled_limits_harness(app, caller_user_id: UUID, config: dict):
    """Patch auth + the meinchat config seam so the limits route runs with
    the plugin enabled and the supplied config."""
    fake_caller = MagicMock()
    fake_caller.id = caller_user_id
    fake_caller.is_admin = False
    fake_caller.status.value = "ACTIVE"

    with ExitStack() as stack:
        patch_user_repo = stack.enter_context(
            patch("vbwd.middleware.auth.UserRepository")
        )
        patch_auth_service = stack.enter_context(
            patch("vbwd.middleware.auth.AuthService")
        )
        stack.enter_context(
            patch(
                "plugins.meinchat.meinchat.routes._meinchat_config",
                return_value=config,
            )
        )
        # Plugin reports enabled to `check_plugin_enabled`.
        enabled_entry = MagicMock()
        enabled_entry.status = "enabled"
        stack.enter_context(
            patch.object(app.config_store, "get_by_name", return_value=enabled_entry)
        )
        stack.enter_context(
            patch.object(app.config_store, "get_config", return_value=config)
        )
        patch_user_repo.return_value.find_by_id.return_value = fake_caller
        patch_auth_service.return_value.verify_token.return_value = str(caller_user_id)
        yield


def _get_limits(client, headers: dict | None = None):
    return client.get(
        _LIMITS_PATH,
        headers={"Authorization": "Bearer test-token", **(headers or {})},
    )


class TestLimitsEndpoint:
    def test_returns_all_four_fields(self, app, client):
        """Spec #1 — all four operator knobs present, each an int."""
        with _enabled_limits_harness(app, uuid4(), _limits_config()):
            response = _get_limits(client)
        assert response.status_code == 200
        body = response.get_json()
        assert set(body.keys()) == _FOUR_KEYS
        for key in _FOUR_KEYS:
            assert isinstance(body[key], int), f"{key} must serialize as int"

    def test_reflects_admin_changed_config(self, app, client):
        """Spec #2 — flipping the config in the store surfaces next call."""
        changed = _limits_config(messages_retention_days_server=7)
        with _enabled_limits_harness(app, uuid4(), changed):
            response = _get_limits(client)
        assert response.status_code == 200
        assert response.get_json()["messages_retention_days_server"] == 7

    def test_auth_required(self, client):
        """Spec #3 — no bearer token → 401."""
        response = client.get(_LIMITS_PATH)
        assert response.status_code == 401

    def test_plugin_disabled_returns_404(self, app, client):
        """Spec #4 — meinchat disabled → standard `Plugin not enabled` 404."""
        fake_caller = MagicMock()
        fake_caller.id = uuid4()
        fake_caller.is_admin = False
        fake_caller.status.value = "ACTIVE"
        with ExitStack() as stack:
            patch_user_repo = stack.enter_context(
                patch("vbwd.middleware.auth.UserRepository")
            )
            patch_auth_service = stack.enter_context(
                patch("vbwd.middleware.auth.AuthService")
            )
            disabled_entry = MagicMock()
            disabled_entry.status = "disabled"
            stack.enter_context(
                patch.object(
                    app.config_store, "get_by_name", return_value=disabled_entry
                )
            )
            patch_user_repo.return_value.find_by_id.return_value = fake_caller
            patch_auth_service.return_value.verify_token.return_value = str(
                fake_caller.id
            )
            response = _get_limits(client)
        assert response.status_code == 404
        assert response.get_json() == {"error": "Plugin not enabled"}

    def test_response_shape_has_exactly_four_fields(self, app, client):
        """Spec #5 — exactly the four operator knobs, and NOT
        `enabled_protocols` (that lives on /messaging/capabilities from
        S28.3a — DRY check)."""
        with _enabled_limits_harness(app, uuid4(), _limits_config()):
            response = _get_limits(client)
        body = response.get_json()
        assert set(body.keys()) == _FOUR_KEYS
        assert "enabled_protocols" not in body

    @pytest.mark.skip(
        reason="DEFERRED to S28.3b: meinchat-plus does not exist in phase 1. "
        "The phase-1 master doc (s28-phase1-retention-and-config.md §3) "
        "explicitly ALLOWS messages_retention_days_server=0 in phase 1; the "
        "IncompatibleRetentionConfigError refusal lands at meinchat-plus "
        "enable time in phase 2 (S28.3a/b). Implementing the loader refusal "
        "now — with no meinchat-plus to gate — would be premature/"
        "overengineering."
    )
    def test_zero_days_with_meinchat_plus_refuses_enable(self):  # pragma: no cover
        """Spec #6 — with meinchat-plus enabled and
        messages_retention_days_server=0, the plugin loader raises
        IncompatibleRetentionConfigError at enable time (closes
        critical-review §C2). See skip reason."""
        raise NotImplementedError
