"""S(install) — ``meinchat.populate_db`` exposes the dev-install contract.

The ``dev-install-ce.sh`` fallback runner does, inside its own app context::

    from plugins.meinchat.populate_db import populate_db
    populate_db()

so the module MUST expose a module-level callable ``populate_db()`` that runs the
full demo seed *assuming an active Flask app context* (it must NOT create its own
app — ``main()`` is the self-bootstrapping entrypoint for the platform installer's
``__main__`` path). This guards that contract (import-level) AND proves the
in-context call imports the ``meinchat-bot-widget`` cms widget idempotently.

Real Postgres. Conforms to the meinchat conftest's rollback-isolation model
(``rollback_isolation``): a test runs inside one transaction the conftest rolls
back, so this suite must NOT open a second top-level ``engine.begin()`` (that is
what broke the older ``engine.begin``-based fixtures). The ``cms_widget`` table
is created on the session's own connection inside the test transaction, and the
``cms_widgets`` exchanger is registered against ``db.session`` — mirroring
``CmsPlugin.on_enable``. cms is a SOFT dependency.

Engineering requirements (binding, restated): TDD-first; DevOps-first (cold
local + CI); SOLID/DI/DRY (one JSON source, shared exchanger; ``populate_db`` is
the single in-context body ``main`` also drives); Liskov; no overengineering.
Quality guard: ``bin/pre-commit-check.sh --plugin meinchat --full``.
"""
import pytest

from vbwd.services.data_exchange.registry import data_exchange_registry

from plugins.meinchat import populate_db as meinchat_populate_module

cms_widget_module = pytest.importorskip("plugins.cms.src.models.cms_widget")
CmsWidget = cms_widget_module.CmsWidget

_WIDGET_SLUG = "meinchat-bot-widget"


@pytest.fixture
def widget_seed_env(app, _isolate_test):
    """Make the live widget-import path reachable inside the conftest's rolled-back
    transaction: create the ``cms_widget`` table on the session connection (so it
    is visible to this transaction and discarded on rollback) and register the
    ``cms_widgets`` exchanger against ``db.session`` (mirroring
    ``CmsPlugin.on_enable``). Yields the session-bound ``db`` handle."""
    from vbwd.extensions import db as _db
    from vbwd.interfaces.file_storage import InMemoryFileStorage
    from plugins.cms.src.services.data_exchange.cms_exchangers import (
        register_cms_exchangers,
    )

    connection = _db.session.connection()
    CmsWidget.__table__.create(bind=connection, checkfirst=True)
    register_cms_exchangers(_db.session, file_storage=InMemoryFileStorage())
    try:
        yield _db
    finally:
        data_exchange_registry.unregister("cms_widgets")


def _widgets(db):
    return db.session.query(CmsWidget).filter_by(slug=_WIDGET_SLUG).all()


def test_module_exposes_callable_populate_db():
    """The dev-install fallback imports this exact name — guard against
    regression (the module previously only exposed ``main``)."""
    assert callable(getattr(meinchat_populate_module, "populate_db", None))


def test_populate_db_seeds_bot_widget(widget_seed_env):
    meinchat_populate_module.populate_db()

    rows = _widgets(widget_seed_env)
    assert len(rows) == 1
    assert rows[0].config["component_name"] == "MeinchatChatWidget"


def test_populate_db_is_idempotent(widget_seed_env):
    meinchat_populate_module.populate_db()
    meinchat_populate_module.populate_db()

    assert len(_widgets(widget_seed_env)) == 1
