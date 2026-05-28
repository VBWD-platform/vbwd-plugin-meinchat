"""Tests for RateLimitPolicy — (category, platform) → (per_window, window_seconds).

Pure unit tests on plain dicts. No Flask, no Redis.
"""
import pytest

from plugins.meinchat.meinchat.services.rate_limit_policy import (
    RateLimitPolicy,
    UnknownRateLimitCategory,
)


def _baseline_config():
    return {
        "rate_new_conversation_per_window": 10,
        "rate_new_conversation_window_seconds": 3600,
        "rate_nickname_search_per_window": 30,
        "rate_nickname_search_window_seconds": 60,
        "rate_message_send_per_window": 30,
        "rate_message_send_window_seconds": 60,
        "rate_attachment_send_per_window": 6,
        "rate_attachment_send_window_seconds": 3600,
    }


def _ios_override_config():
    cfg = _baseline_config()
    cfg.update(
        {
            "rate_ios_new_conversation_per_window": 60,
            "rate_ios_new_conversation_window_seconds": 3600,
            "rate_ios_nickname_search_per_window": 90,
            "rate_ios_nickname_search_window_seconds": 60,
            "rate_ios_message_send_per_window": 120,
            "rate_ios_message_send_window_seconds": 60,
            "rate_ios_attachment_send_per_window": 30,
            "rate_ios_attachment_send_window_seconds": 3600,
        }
    )
    return cfg


class TestRateLimitPolicy:
    def test_baseline_for_web_platform(self):
        policy = RateLimitPolicy(_baseline_config())
        assert policy.limits_for("new_conversation", "web") == (10, 3600)
        assert policy.limits_for("nickname_search", "web") == (30, 60)
        assert policy.limits_for("message_send", "web") == (30, 60)
        assert policy.limits_for("attachment_send", "web") == (6, 3600)

    def test_baseline_for_unknown_platform(self):
        policy = RateLimitPolicy(_ios_override_config())
        # android isn't configured → falls through to baseline, not the iOS bump
        assert policy.limits_for("new_conversation", "android") == (10, 3600)

    def test_ios_override_wins_when_present(self):
        policy = RateLimitPolicy(_ios_override_config())
        assert policy.limits_for("new_conversation", "ios") == (60, 3600)
        assert policy.limits_for("message_send", "ios") == (120, 60)
        assert policy.limits_for("attachment_send", "ios") == (30, 3600)

    def test_ios_falls_through_to_baseline_for_unconfigured_category(self):
        cfg = _ios_override_config()
        # remove the iOS keys for nickname_search → must fall through
        cfg.pop("rate_ios_nickname_search_per_window")
        cfg.pop("rate_ios_nickname_search_window_seconds")
        policy = RateLimitPolicy(cfg)
        assert policy.limits_for("nickname_search", "ios") == (30, 60)

    def test_unknown_category_raises_value_error(self):
        policy = RateLimitPolicy(_baseline_config())
        with pytest.raises(UnknownRateLimitCategory):
            policy.limits_for("typo_category", "web")

    def test_legacy_message_rate_per_minute_back_compat(self):
        # Pre-S26 instance only has the legacy flat key — must still produce
        # a usable limit so an upgrade doesn't silently reset tuning.
        cfg = {"message_rate_per_minute": 50}
        policy = RateLimitPolicy(cfg)
        assert policy.limits_for("message_send", "web") == (50, 60)

    def test_legacy_attachment_rate_per_hour_back_compat(self):
        cfg = {"attachment_rate_per_hour": 12}
        policy = RateLimitPolicy(cfg)
        assert policy.limits_for("attachment_send", "web") == (12, 3600)

    def test_zero_or_negative_pair_ignored_in_favour_of_fallback(self):
        # Defensive: even if admin-config min:1 guard is bypassed, the policy
        # must never hand back a 0/0 (which would lock all users out).
        cfg = {
            "rate_new_conversation_per_window": 0,
            "rate_new_conversation_window_seconds": -5,
        }
        policy = RateLimitPolicy(cfg)
        assert policy.limits_for("new_conversation", "web") == (10, 3600)

    def test_partial_baseline_pair_ignored(self):
        # Only one of the two keys present → treat as missing, not as
        # a half-configured limit.
        cfg = {"rate_new_conversation_per_window": 7}
        policy = RateLimitPolicy(cfg)
        assert policy.limits_for("new_conversation", "web") == (10, 3600)

    def test_platform_string_normalised(self):
        # Header arrives as "iOS" sometimes; lowercase before lookup.
        policy = RateLimitPolicy(_ios_override_config())
        assert policy.limits_for("new_conversation", "iOS") == (60, 3600)
        assert policy.limits_for("new_conversation", "IOS") == (60, 3600)

    def test_empty_platform_treated_as_web(self):
        policy = RateLimitPolicy(_ios_override_config())
        assert policy.limits_for("new_conversation", "") == (10, 3600)
