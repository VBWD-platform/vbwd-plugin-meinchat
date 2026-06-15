"""Shared fixtures for meinchat tests.

Provides the `app` and `client` fixtures `pytest-flask` looks up by name
when route specs ask for them. Service-level specs use `MagicMock`
collaborators and don't need this — the fixture only loads when a test
actually requests it.
"""
import os
import sys

import pytest


# Ensure the project root is on sys.path so plugin modules import as
# `plugins.meinchat.…` regardless of where pytest was invoked from.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("TESTING", "true")


def _test_db_url() -> str:
    """Use a sibling `<dbname>_test` database so the integration tests don't
    collide with whatever the api container is doing in the main `vbwd` DB."""
    base = os.getenv("DATABASE_URL", "postgresql://vbwd:vbwd@postgres:5432/vbwd")
    prefix, _, dbname = base.rpartition("/")
    dbname = dbname.split("?")[0]
    return f"{prefix}/{dbname}_test"


def _ensure_test_db(url: str) -> None:
    from sqlalchemy import create_engine, text

    main_url = url.rsplit("/", 1)[0] + "/postgres"
    dbname = url.rsplit("/", 1)[1].split("?")[0]
    engine = create_engine(main_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": dbname}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def app():
    """Boot the full Flask app on a `<dbname>_test` database with the full
    schema built once for the session via the shared integration-db helper.
    Self-bootstrapping so the test doesn't depend on the api container's CMD
    having finished `alembic upgrade heads`.

    Each test isolates its own data via the autouse ``_isolate_test`` fixture
    (TRUNCATE on setup). Previously the suite assumed rows seeded by one spec
    were visible to the next; that assumption is incompatible with sharing the
    ``*_test`` DB (a sibling suite's mid-session DROP SCHEMA would wipe the
    rows), so the one dependent spec was made self-seeding instead."""
    from vbwd.app import create_app
    from vbwd.extensions import db as _db

    test_url = _test_db_url()
    _ensure_test_db(test_url)
    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": test_url,
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            "WTF_CSRF_ENABLED": False,
            "RATELIMIT_ENABLED": False,
            "RATELIMIT_STORAGE_URL": "memory://",
        }
    )
    with application.app_context():
        # Importing the meinchat models package registers user_nickname,
        # user_contact, conversation, message, token_transfer with SQLAlchemy
        # so they get created alongside the core tables.
        import plugins.meinchat.meinchat.models  # noqa: F401

        # meinchat-plus is enabled per-instance; when it is, its `on_enable`
        # registers a device directory backed by these tables, which the
        # capabilities/negotiation paths query. Create them too so those tests
        # don't hit a missing-table error. Guarded so meinchat tests still run
        # if the sibling plugin is absent (independent-repo posture).
        try:
            import plugins.meinchat_plus.meinchat_plus.models  # noqa: F401
        except ImportError:
            pass

        # Build the schema once per process (create_all, checkfirst — never
        # drops, so it cannot wipe data) and commit baseline reference rows
        # once. Each test then isolates itself via a rolled-back transaction
        # (no TRUNCATE, no DROP) — see vbwd/testing/integration_db.py.
        from vbwd.testing.integration_db import ensure_schema_and_baseline

        ensure_schema_and_baseline(_db)

    yield application

    with application.app_context():
        _db.engine.dispose()


@pytest.fixture(autouse=True)
def _isolate_test(app, request):
    """Isolate every test in a rolled-back transaction (self-cleaning, no wipe).

    meinchat specs seed their own users/nicknames; nothing a test writes persists
    past it (the rollback IS the cleanup), so each test is deterministic and
    order-independent. Autouse so client-only route specs are isolated too. The
    schema + canonical RBAC role rows are built once in the ``app`` fixture, so a
    User created inside the test's transaction satisfies its role FK. See
    vbwd/testing/integration_db.py.

    A test marked ``no_db_isolation`` (e.g. a migration spec that opens its own
    connection and rolls back itself) runs WITHOUT the wrapper, keeping
    ``db.engine`` a real Engine.
    """
    from vbwd.extensions import db as _db

    if request.node.get_closest_marker("no_db_isolation") is not None:
        with app.app_context():
            yield
            _db.session.remove()
        return

    with app.app_context():
        from vbwd.testing.integration_db import rollback_isolation

        with rollback_isolation(_db):
            yield


@pytest.fixture
def client(app):
    return app.test_client()
