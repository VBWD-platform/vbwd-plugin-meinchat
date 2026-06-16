"""S86.3 D11 — the economy config reads MUST fall back to the plugin's
DEFAULT_CONFIG when a key is absent from the persisted per-plugin override.

The bug: `_meinchat_config()` returns ONLY the persisted overrides (which are
`{}` on a fresh install). The economy reads (`guest_economy_enabled`,
`guest_initial_tokens`, `guest_token_cost_per_word`) used local literal defaults
(0 / wrong) instead of the designed DEFAULT_CONFIG values, so a freshly
provisioned guest was granted 0 tokens and the first send was 402-gated.

These tests drive the REAL `_economy_config()` helper (no patching of the
resolver away) so the DEFAULT fallback path is actually exercised, and confirm a
persisted override still wins.
"""
from unittest.mock import patch

from plugins.meinchat import DEFAULT_CONFIG
from plugins.meinchat.meinchat.routes import _economy_config


def _with_persisted(app, persisted):
    """Make the real config_store return ``persisted`` for meinchat."""
    return patch.object(
        app.config_store,
        "get_config",
        side_effect=lambda name: dict(persisted) if name == "meinchat" else None,
    )


def test_fresh_install_resolves_to_default_config(app):
    """Empty persisted override → DEFAULT_CONFIG economy values."""
    with app.app_context(), _with_persisted(app, {}):
        cfg = _economy_config()
    assert cfg["guest_economy_enabled"] is DEFAULT_CONFIG["guest_economy_enabled"]
    assert cfg["guest_initial_tokens"] == DEFAULT_CONFIG["guest_initial_tokens"] == 20
    assert (
        cfg["guest_token_cost_per_word"]
        == DEFAULT_CONFIG["guest_token_cost_per_word"]
        == 1
    )
    # Default True preserves today's behavior: the guest is charged for the
    # bot's answer words too, with no persisted override on a fresh install.
    assert cfg["guest_charge_bot_answers"] is True
    assert cfg["guest_charge_bot_answers"] is DEFAULT_CONFIG["guest_charge_bot_answers"]


def test_persisted_override_wins_over_default(app):
    """An admin-set override for one knob wins; the others still default."""
    with app.app_context(), _with_persisted(app, {"guest_initial_tokens": 5}):
        cfg = _economy_config()
    assert cfg["guest_initial_tokens"] == 5
    # Untouched knobs still fall back to the documented defaults.
    assert cfg["guest_economy_enabled"] is DEFAULT_CONFIG["guest_economy_enabled"]
    assert (
        cfg["guest_token_cost_per_word"] == DEFAULT_CONFIG["guest_token_cost_per_word"]
    )


def test_disabled_override_is_respected(app):
    """A persisted `guest_economy_enabled=False` must win over the True default."""
    with app.app_context(), _with_persisted(app, {"guest_economy_enabled": False}):
        cfg = _economy_config()
    assert cfg["guest_economy_enabled"] is False
