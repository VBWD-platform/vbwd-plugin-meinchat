"""S26 §4.5 — guard the meinchat plugin config schema.

The 16 rate-limit keys must be present in DEFAULT_CONFIG, documented in
config.json with type/default/description, and exposed in admin-config.json
as editable numeric fields with sensible min/max bounds.
"""
import json
import os
from typing import Dict

import pytest


_PLUGIN_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)


_RATE_KEYS_BASELINE = [
    "rate_new_conversation_per_window",
    "rate_new_conversation_window_seconds",
    "rate_nickname_search_per_window",
    "rate_nickname_search_window_seconds",
    "rate_message_send_per_window",
    "rate_message_send_window_seconds",
    "rate_attachment_send_per_window",
    "rate_attachment_send_window_seconds",
]
_RATE_KEYS_IOS = [
    "rate_ios_new_conversation_per_window",
    "rate_ios_new_conversation_window_seconds",
    "rate_ios_nickname_search_per_window",
    "rate_ios_nickname_search_window_seconds",
    "rate_ios_message_send_per_window",
    "rate_ios_message_send_window_seconds",
    "rate_ios_attachment_send_per_window",
    "rate_ios_attachment_send_window_seconds",
]
_RATE_KEYS_ALL = _RATE_KEYS_BASELINE + _RATE_KEYS_IOS


_EXPECTED_DEFAULTS: Dict[str, int] = {
    "rate_new_conversation_per_window": 10,
    "rate_new_conversation_window_seconds": 3600,
    "rate_nickname_search_per_window": 30,
    "rate_nickname_search_window_seconds": 60,
    "rate_message_send_per_window": 30,
    "rate_message_send_window_seconds": 60,
    "rate_attachment_send_per_window": 6,
    "rate_attachment_send_window_seconds": 3600,
    "rate_ios_new_conversation_per_window": 60,
    "rate_ios_new_conversation_window_seconds": 3600,
    "rate_ios_nickname_search_per_window": 90,
    "rate_ios_nickname_search_window_seconds": 60,
    "rate_ios_message_send_per_window": 120,
    "rate_ios_message_send_window_seconds": 60,
    "rate_ios_attachment_send_per_window": 30,
    "rate_ios_attachment_send_window_seconds": 3600,
}


@pytest.fixture(scope="module")
def config_schema() -> dict:
    with open(os.path.join(_PLUGIN_DIR, "config.json"), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def admin_config_schema() -> dict:
    with open(
        os.path.join(_PLUGIN_DIR, "admin-config.json"), encoding="utf-8"
    ) as fh:
        return json.load(fh)


class TestDefaultConfig:
    def test_default_config_includes_all_16_rate_keys(self):
        from plugins.meinchat import DEFAULT_CONFIG

        for key, expected_default in _EXPECTED_DEFAULTS.items():
            assert key in DEFAULT_CONFIG, (
                f"DEFAULT_CONFIG missing required key {key}"
            )
            assert DEFAULT_CONFIG[key] == expected_default, (
                f"DEFAULT_CONFIG[{key}] = {DEFAULT_CONFIG[key]!r}, "
                f"expected {expected_default!r}"
            )


class TestConfigJsonSchema:
    def test_documents_all_16_rate_keys(self, config_schema):
        for key in _RATE_KEYS_ALL:
            assert key in config_schema, (
                f"config.json missing schema entry for {key}"
            )
            entry = config_schema[key]
            assert entry["type"] == "integer"
            assert isinstance(entry["default"], int)
            assert entry["description"], (
                f"config.json[{key}] must carry a non-empty description"
            )

    def test_defaults_match_default_config(self, config_schema):
        # The two homes for defaults must agree — config.json is the
        # documented schema; DEFAULT_CONFIG is what the plugin merges at
        # runtime.
        for key, expected in _EXPECTED_DEFAULTS.items():
            assert config_schema[key]["default"] == expected


class TestAdminConfigJson:
    def test_has_rate_limit_tabs(self, admin_config_schema):
        tab_ids = {tab["id"] for tab in admin_config_schema["tabs"]}
        assert "rate-limits-baseline" in tab_ids
        assert "rate-limits-ios" in tab_ids

    def test_baseline_tab_has_8_numeric_fields(self, admin_config_schema):
        baseline_tab = next(
            tab for tab in admin_config_schema["tabs"]
            if tab["id"] == "rate-limits-baseline"
        )
        fields_by_key = {f["key"]: f for f in baseline_tab["fields"]}
        assert set(fields_by_key.keys()) == set(_RATE_KEYS_BASELINE)
        for key, field in fields_by_key.items():
            assert field["component"] == "input"
            assert field["inputType"] == "number"
            assert field.get("min", 0) >= 1, (
                f"{key} must have min >= 1 (zero would lock out users)"
            )
            assert "max" in field, f"{key} must declare a max"

    def test_ios_tab_has_8_numeric_fields(self, admin_config_schema):
        ios_tab = next(
            tab for tab in admin_config_schema["tabs"]
            if tab["id"] == "rate-limits-ios"
        )
        fields_by_key = {f["key"]: f for f in ios_tab["fields"]}
        assert set(fields_by_key.keys()) == set(_RATE_KEYS_IOS)
        for key, field in fields_by_key.items():
            assert field["component"] == "input"
            assert field["inputType"] == "number"
            assert field.get("min", 0) >= 1
            assert "max" in field
