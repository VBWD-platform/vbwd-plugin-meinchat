"""Unit guards for the meinchat demo bot-widget seeder.

The seeder now imports the shipped widget JSON through the unified
data-exchange mechanism (the JSON is the single source of truth) instead of a
hardcoded dict. These unit tests guard two contracts without touching a DB:

  1. The shipped envelope on disk is a valid ``cms_widgets`` VBWD envelope
     (``validate_envelope`` passes) and carries the canonical
     ``meinchat-bot-widget`` slug — a future malformed edit fails here.
  2. Soft-dependency on cms: when no ``cms_widgets`` exchanger is registered
     (cms absent/disabled), the seeder skips cleanly without raising.

Engineering requirements (binding, restated): TDD-first; DevOps-first (cold
local + CI); SOLID/DI/DRY (the JSON is the one source; reuse the shared
envelope validator + registry); Liskov; no overengineering. Quality guard:
``bin/pre-commit-check.sh --plugin meinchat --full``.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

from vbwd.services.data_exchange.envelope import validate_envelope
from vbwd.services.data_exchange.registry import data_exchange_registry

from plugins.meinchat import populate_db


_WIDGET_JSON = (
    Path(populate_db.__file__).resolve().parent
    / "docs/import/cms/widgets/meinchat-bot-widget.json"
)


def test_shipped_widget_envelope_is_valid():
    payload = json.loads(_WIDGET_JSON.read_text(encoding="utf-8"))

    rows = validate_envelope(payload, "cms_widgets")

    assert len(rows) == 1
    assert rows[0]["slug"] == "meinchat-bot-widget"
    assert rows[0]["widget_type"] == "vue-component"
    assert rows[0]["config"]["component_name"] == "MeinchatChatWidget"


def test_seed_skips_when_cms_widgets_exchanger_absent():
    data_exchange_registry.unregister("cms_widgets")

    # No exchanger registered → the seeder must skip cleanly (soft dep on cms),
    # never touching the DB session it was handed.
    db = MagicMock()
    populate_db._seed_demo_widget(db)

    db.session.commit.assert_not_called()
