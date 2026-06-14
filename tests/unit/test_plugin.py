"""S28.0 §2.3 — guard the four retention/size knobs in DEFAULT_CONFIG.

The `/api/v1/messaging/limits` route, the S28.1 server prune, and the
S28.2 client-cache TTL resolver all read these four keys from the single
config store. DEFAULT_CONFIG is the runtime source of truth that the
plugin's `initialize()` merges, so the four keys must ship there with
their documented defaults.
"""
from typing import Dict
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _restore_meinchat_registry():
    """`on_enable()` registers extension impls (incl. the D11 charge
    ``IPostSendHook``) into the shared in-process registry. These unit tests
    call `on_enable()` directly; without restoration their registrations leak
    into later (esp. integration) tests — a stale DUPLICATE charge hook then
    silently double-debits guest tokens. Snapshot the registry and restore it
    afterwards so this file's `on_enable` calls leave it exactly as the
    session app set it up (rather than wiping the session's own impls)."""
    from plugins.meinchat.meinchat.extensibility import registry

    snapshot = registry.snapshot_for_tests()
    yield
    registry.restore_for_tests(snapshot)


_EXPECTED_RETENTION_DEFAULTS: Dict[str, int] = {
    "messages_retention_days_server": 2,
    "messages_retention_days_client_suggested": 10,
    "attachments_retention_days_server": 2,
    "ciphertext_max_bytes": 16384,
}


class TestRetentionDefaults:
    def test_default_config_ships_four_retention_keys(self):
        from plugins.meinchat import DEFAULT_CONFIG

        for key, expected_default in _EXPECTED_RETENTION_DEFAULTS.items():
            assert key in DEFAULT_CONFIG, f"DEFAULT_CONFIG missing required key {key}"
            assert DEFAULT_CONFIG[key] == expected_default, (
                f"DEFAULT_CONFIG[{key}] = {DEFAULT_CONFIG[key]!r}, "
                f"expected {expected_default!r}"
            )


class TestSchedulerTestingGuard:
    """S28.1 §3.3 — regression for the `--full` connection-pool exhaustion
    lesson (feedback_ci_precommit_lessons): the retention scheduler must NOT
    spawn when ``app.config['TESTING']`` is true, or it leaks threads + PG
    connection slots across the ~1900-test run."""

    def _enable_plugin(self, testing_flag):
        from plugins.meinchat import MeinchatPlugin

        plugin = MeinchatPlugin()
        plugin.initialize()

        fake_app = MagicMock()
        fake_app.config = {"TESTING": testing_flag}
        fake_app.container = None  # skip repo registration in this unit test

        with patch(
            "plugins.meinchat.meinchat.scheduler.start_retention_scheduler"
        ) as start_mock, patch("plugins.meinchat.current_app", fake_app):
            plugin.on_enable()
        return start_mock

    def test_scheduler_not_registered_when_testing(self):
        start_mock = self._enable_plugin(testing_flag=True)
        start_mock.assert_not_called()

    def test_scheduler_registered_when_not_testing(self):
        start_mock = self._enable_plugin(testing_flag=False)
        start_mock.assert_called_once()


class TestCmsWidgetReaderRegistration:
    """S86.3 D2 — cms is a SOFT dependency: meinchat enables + resolves a widget
    reader whether or not cms is importable. With cms ABSENT only the null
    reader is registered (widget-start 404s); meinchat never hard-depends."""

    def _enable(self):
        from plugins.meinchat import MeinchatPlugin

        plugin = MeinchatPlugin()
        plugin.initialize()
        fake_app = MagicMock()
        fake_app.config = {"TESTING": True}
        fake_app.container = None
        with patch("plugins.meinchat.current_app", fake_app):
            plugin.on_enable()

    def test_cms_absent_leaves_only_null_reader(self):
        from plugins.meinchat.meinchat.extensibility import registry
        from plugins.meinchat.meinchat.extensibility.cms_widget_reader import (
            ICmsWidgetReader,
            NullCmsWidgetReader,
        )

        registry.reset_for_tests(ICmsWidgetReader)
        # Simulate cms being absent: the guarded `import plugins.cms` raises.
        real_import = __import__

        def _fail_cms(name, *args, **kwargs):
            if name == "plugins.cms" or name.startswith("plugins.cms."):
                raise ImportError("cms not installed")
            return real_import(name, *args, **kwargs)

        try:
            with patch("builtins.__import__", side_effect=_fail_cms):
                self._enable()
            reader = registry.resolve_first(ICmsWidgetReader)
            assert isinstance(reader, NullCmsWidgetReader)
            assert reader.get_active_widget_config("any") is None
        finally:
            registry.reset_for_tests(ICmsWidgetReader)

    def test_cms_present_registers_cms_backed_reader_last(self):
        from plugins.meinchat.meinchat.extensibility import registry
        from plugins.meinchat.meinchat.extensibility.cms_widget_reader import (
            CmsWidgetReader,
            ICmsWidgetReader,
        )

        registry.reset_for_tests(ICmsWidgetReader)
        try:
            self._enable()
            # cms is importable in the monorepo → the cms-backed adapter wins.
            reader = registry.resolve_first(ICmsWidgetReader)
            assert isinstance(reader, CmsWidgetReader)
        finally:
            registry.reset_for_tests(ICmsWidgetReader)
