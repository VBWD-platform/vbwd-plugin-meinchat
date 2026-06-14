"""Unit tests for the ICmsWidgetReader port + its null / cms-backed impls
(S86.3, D2). The cms-backed adapter is exercised through a fake repository so
no cms import / DB is required here; the guarded-import behaviour (cms absent →
null) is covered by the plugin-level cms-absent test and integration.
"""
from types import SimpleNamespace

from plugins.meinchat.meinchat.extensibility.cms_widget_reader import (
    CmsWidgetReader,
    ICmsWidgetReader,
    NullCmsWidgetReader,
)


def test_null_reader_returns_none_and_satisfies_port():
    reader = NullCmsWidgetReader()
    assert isinstance(reader, ICmsWidgetReader)
    assert reader.get_active_widget_config("anything") is None


class _FakeWidget:
    def __init__(self, *, is_active=True, widget_type="vue-component", config=None):
        self.is_active = is_active
        self.widget_type = widget_type
        self.config = config or {}
        self.content_json = {}


def _reader_for(widget):
    repo = SimpleNamespace(find_by_slug=lambda slug: widget)
    reader = CmsWidgetReader(session_provider=lambda: None)
    # Inject the fake repo by overriding the guarded builder for the test.
    reader._build_repository = lambda: repo  # type: ignore[method-assign]
    return reader


def test_cms_reader_returns_config_for_active_meinchat_widget():
    widget = _FakeWidget(
        config={"component_name": "MeinchatChatWidget", "visibility": "public"}
    )
    reader = _reader_for(widget)
    config = reader.get_active_widget_config("chat")
    assert config == {"component_name": "MeinchatChatWidget", "visibility": "public"}


def test_cms_reader_ignores_inactive_widget():
    widget = _FakeWidget(
        is_active=False, config={"component_name": "MeinchatChatWidget"}
    )
    assert _reader_for(widget).get_active_widget_config("chat") is None


def test_cms_reader_ignores_wrong_widget_type():
    widget = _FakeWidget(
        widget_type="html", config={"component_name": "MeinchatChatWidget"}
    )
    assert _reader_for(widget).get_active_widget_config("chat") is None


def test_cms_reader_ignores_wrong_component():
    widget = _FakeWidget(config={"component_name": "SomethingElse"})
    assert _reader_for(widget).get_active_widget_config("chat") is None


def test_cms_reader_returns_none_for_missing_slug():
    reader = _reader_for(None)
    assert reader.get_active_widget_config("nope") is None


def test_cms_reader_reads_component_from_content_json_fallback():
    widget = _FakeWidget(config={"visibility": "public"})
    widget.content_json = {"component": "MeinchatChatWidget"}
    config = _reader_for(widget).get_active_widget_config("chat")
    assert config == {"visibility": "public"}
