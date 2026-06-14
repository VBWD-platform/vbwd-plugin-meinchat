"""Integration: the meinchat seeder imports the bot-widget JSON (real PG).

``_seed_demo_widget`` no longer hardcodes a widget dict — it loads the shipped
``docs/import/cms/widgets/meinchat-bot-widget.json`` envelope, validates it, and
imports it through the registered ``cms_widgets`` data-exchange exchanger in
``mode="upsert"`` (idempotent by slug). This proves the live path against a real
Postgres: cms present + the exchanger registered → one ``cms_widget`` row with
the canonical slug + config, and a second run upserts (no duplicate, no error).

cms is a SOFT dependency: this suite imports the cms models / exchanger and
registers it explicitly (the same wiring ``CmsPlugin.on_enable`` does), so the
seeder finds the exchanger via the shared registry. Data is seeded through the
exchanger / repository layer (no raw SQL) per feedback_no_direct_db_for_test_data.

Engineering requirements (binding, restated): TDD-first; DevOps-first (cold
local + CI); SOLID/DI/DRY (one JSON source, shared exchanger); Liskov; no
overengineering. Quality guard: ``bin/pre-commit-check.sh --plugin meinchat
--full``.
"""
import pytest

from vbwd.services.data_exchange.registry import data_exchange_registry

from plugins.meinchat import populate_db

cms_widget_module = pytest.importorskip("plugins.cms.src.models.cms_widget")
CmsWidget = cms_widget_module.CmsWidget

_WIDGET_SLUG = "meinchat-bot-widget"


@pytest.fixture
def db(app):
    """Function-scoped DB with the ``cms_widget`` table created and the
    ``cms_widgets`` exchanger registered into the shared registry — mirroring
    ``CmsPlugin.on_enable`` so the seeder's unified-import path is live.

    The session-scoped ``app`` fixture already ran ``create_all()`` for core +
    meinchat models (and created the shared enums), so we create ONLY the
    ``cms_widget`` table here (``checkfirst`` makes a repeat run a no-op) and
    truncate it per test for isolation. Re-running ``create_all()`` would clash
    on the already-created shared enum types."""
    from sqlalchemy import text

    from vbwd.extensions import db as _db
    from vbwd.interfaces.file_storage import InMemoryFileStorage
    from plugins.cms.src.services.data_exchange.cms_exchangers import (
        register_cms_exchangers,
    )

    with app.app_context():
        # Ensure the cms_widget table exists (no-op when cms tests already
        # created it), then start from empty on its own committed connection.
        # The cms enums/tables are not part of the session-scoped meinchat
        # schema, so a plain create_all would clash on shared enums; a TRUNCATE
        # avoids dropping the table that cms FKs depend on.
        with _db.engine.begin() as connection:
            CmsWidget.__table__.create(bind=connection, checkfirst=True)
            connection.execute(text("TRUNCATE TABLE cms_widget CASCADE"))
        register_cms_exchangers(_db.session, file_storage=InMemoryFileStorage())
        try:
            yield _db
        finally:
            data_exchange_registry.unregister("cms_widgets")
            _db.session.remove()
            with _db.engine.begin() as connection:
                connection.execute(text("TRUNCATE TABLE cms_widget CASCADE"))


def _widgets(db):
    return db.session.query(CmsWidget).filter_by(slug=_WIDGET_SLUG).all()


def test_seed_imports_widget_json_via_unified_exchange(db):
    populate_db._seed_demo_widget(db)

    rows = _widgets(db)
    assert len(rows) == 1
    widget = rows[0]
    assert widget.widget_type == "vue-component"
    assert widget.is_active is True
    assert widget.config["component_name"] == "MeinchatChatWidget"
    assert widget.config["visibility"] == "public"
    assert widget.config["member_nicknames"] == ["assistant"]


def test_seed_is_idempotent_upsert(db):
    populate_db._seed_demo_widget(db)
    populate_db._seed_demo_widget(db)

    assert len(_widgets(db)) == 1
