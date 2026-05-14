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


@pytest.fixture
def app():
    """Boot the full Flask app once per test that requests the fixture."""
    from vbwd.app import create_app
    from vbwd.config import get_database_url

    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": get_database_url(),
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    yield application


@pytest.fixture
def client(app):
    return app.test_client()
