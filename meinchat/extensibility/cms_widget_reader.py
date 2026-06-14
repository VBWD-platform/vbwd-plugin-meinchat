"""CMS widget reader port (S86.3, D2) — a SOFT dependency on the cms plugin.

The bot-widget's behaviour-defining config (member nicknames + visibility) is
SERVER-TRUSTED: it is read from the stored cms widget row, never from the
request body (AD10). meinchat does NOT hard-depend on cms — messaging works
fully without it. Only the widget-start endpoint needs the cms row, so the
dependency is a narrow port:

- ``NullCmsWidgetReader`` is the registry default; with cms absent/disabled it
  returns ``None`` for every slug, so ``/messaging/widget/start`` answers a
  clean ``404 widget_not_found`` (Liskov: the disabled-plugin path does not
  break the caller).
- ``CmsWidgetReader`` is wired only when cms is importable; it does a guarded
  import of the cms widget repository and reads the active
  ``MeinchatChatWidget`` (``vue-component``) row by slug.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Protocol, runtime_checkable

# The widget kind this reader recognises — a CMS ``vue-component`` widget whose
# stored component name is ``MeinchatChatWidget`` (D1/D3).
MEINCHAT_WIDGET_COMPONENT = "MeinchatChatWidget"
_VUE_COMPONENT_WIDGET_TYPE = "vue-component"


@runtime_checkable
class ICmsWidgetReader(Protocol):
    """Narrow read port: resolve a bot-widget's server-trusted config by slug."""

    def get_active_widget_config(self, slug: str) -> Optional[Dict[str, Any]]:
        """Return the config dict of the ACTIVE ``MeinchatChatWidget`` widget
        with this slug, or ``None`` when no such active widget exists (unknown
        slug, inactive row, wrong widget type/component, or cms absent)."""
        ...


class NullCmsWidgetReader:
    """Default reader used when cms is absent or disabled — every slug is
    unknown, so widget-start answers ``404 widget_not_found``."""

    def get_active_widget_config(self, slug: str) -> Optional[Dict[str, Any]]:
        return None


class CmsWidgetReader:
    """cms-backed adapter. Guarded-imports the cms widget repository so meinchat
    never hard-depends on cms at import time; if the import fails it behaves
    exactly like ``NullCmsWidgetReader`` (Liskov)."""

    def __init__(self, session_provider) -> None:
        """``session_provider`` is a zero-arg callable returning the active
        SQLAlchemy session (request-scoped), kept as a callable so the reader
        does not capture a stale session."""
        self._session_provider = session_provider

    def get_active_widget_config(self, slug: str) -> Optional[Dict[str, Any]]:
        repository = self._build_repository()
        if repository is None:
            return None
        widget = repository.find_by_slug(slug)
        if widget is None or not widget.is_active:
            return None
        if widget.widget_type != _VUE_COMPONENT_WIDGET_TYPE:
            return None
        config = widget.config or {}
        content = widget.content_json or {}
        component_name = config.get("component_name") or content.get("component")
        if component_name != MEINCHAT_WIDGET_COMPONENT:
            return None
        return config

    def _build_repository(self):
        try:
            from plugins.cms.src.repositories.cms_widget_repository import (
                CmsWidgetRepository,
            )
        except ImportError:
            return None
        return CmsWidgetRepository(self._session_provider())
