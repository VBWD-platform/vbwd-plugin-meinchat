"""Idempotent demo seed for the meinchat plugin.

Run inside the api container:

    docker compose exec api python /app/plugins/meinchat/populate_db.py

What it does (each step is a no-op when state already matches):

  1. Ensure two demo users exist (`alice@example.com`, `bob@example.com`)
     via UserService — never a raw INSERT.
  2. Set their nicknames `alice` and `bob` via NicknameService.
  3. Add `bob` to alice's contacts (alias "Bobby", pinned).
  4. Top up alice's token balance to 200 via TokenService.credit_tokens.
  5. Open a conversation alice ↔ bob and post one text + one
     token_transfer system message via the proper services.

This script is the single source of test/demo data for both local smoke
tests AND CI runs — see `feedback_no_direct_db_for_test_data.md`. Any
new demo state must extend this file, not bypass it with raw SQL.
"""
import os
import sys


def _ensure_app_context():
    """Bootstrap the Flask app + push a request-less context so service
    instantiation has access to `current_app.container` and `db.session`."""
    sys.path.insert(0, "/app")
    os.environ.setdefault("FLASK_ENV", "development")
    from vbwd.app import create_app

    app = create_app()
    return app


def _ensure_user(user_repo, auth_service, *, email: str, name: str, password: str):
    """Find-or-register via the proper services. AuthService.register hashes
    the password, persists the User, and creates the linked UserDetails row."""
    existing = user_repo.find_by_email(email)
    if existing is not None:
        return existing
    auth_service.register(email=email, password=password)
    user = user_repo.find_by_email(email)
    # auth_service.register doesn't take `name`; set it on UserDetails after
    # creation for a friendlier display in the UI. Leaves the user
    # auth-ready and discoverable.
    if user is not None and hasattr(user, "details") and user.details is not None:
        user.details.name = name
    return user


def populate_db() -> None:
    """Module-level entrypoint matching the dev-install fallback contract
    (``from plugins.meinchat.populate_db import populate_db; populate_db()``).

    Runs the full demo seed *assuming an ACTIVE Flask app context* — it creates
    no app of its own (``main`` is the self-bootstrapping path). Every step is
    idempotent.
    """
    from flask import current_app

    from vbwd.extensions import db
    from vbwd.models.enums import TokenTransactionType

    from plugins.meinchat.meinchat.repositories.contact_repository import (
        ContactRepository,
    )
    from plugins.meinchat.meinchat.repositories.conversation_repository import (
        ConversationRepository,
    )
    from plugins.meinchat.meinchat.repositories.message_repository import (
        MessageRepository,
    )
    from plugins.meinchat.meinchat.repositories.nickname_repository import (
        NicknameRepository,
    )
    from plugins.meinchat.meinchat.services.contact_service import (
        ContactAlreadyExistsError,
        ContactService,
    )
    from plugins.meinchat.meinchat.services.conversation_service import (
        ConversationService,
    )
    from plugins.meinchat.meinchat.services.message_service import MessageService
    from plugins.meinchat.meinchat.services.nickname_service import (
        NicknameService,
        NicknameTakenError,
    )

    container = current_app.container
    user_repo = container.user_repository()
    auth_service = container.auth_service()
    token_service = container.token_service()

    print("── 1. demo users ─────────────────────────────────────────")
    alice = _ensure_user(
        user_repo,
        auth_service,
        email="alice@example.com",
        name="Alice Example",
        password="AlicePass123@",
    )
    bob = _ensure_user(
        user_repo,
        auth_service,
        email="bob@example.com",
        name="Bob Example",
        password="BobPass123@",
    )
    print(f"  alice.id = {alice.id}")
    print(f"  bob.id   = {bob.id}")

    print("── 2. nicknames ──────────────────────────────────────────")
    nickname_service = NicknameService(repo=NicknameRepository(db.session))
    for user, nickname in [(alice, "alice"), (bob, "bob")]:
        try:
            row = nickname_service.set_nickname(user.id, nickname)
            print(f"  set @{row.nickname} for {user.email}")
        except NicknameTakenError:
            # Already taken by this same user (idempotent re-run) — fine.
            existing = nickname_service.get_mine(user.id)
            if existing and existing.nickname == nickname:
                print(f"  @{nickname} already set for {user.email}")
            else:
                raise
    db.session.commit()

    print("── 3. contacts ───────────────────────────────────────────")
    contact_service = ContactService(
        contact_repo=ContactRepository(db.session),
        nickname_repo=NicknameRepository(db.session),
    )
    try:
        contact_service.add_contact(
            alice.id, nickname="bob", alias="Bobby", note="demo peer", pinned=True
        )
        print("  alice → @bob (pinned)")
    except ContactAlreadyExistsError:
        print("  alice → @bob already in contacts")
    db.session.commit()

    print("── 4. token balance ──────────────────────────────────────")
    target_balance = 200
    current = token_service.get_balance(alice.id)
    gap = target_balance - current
    if gap > 0:
        token_service.credit_tokens(
            alice.id,
            gap,
            TokenTransactionType.BONUS,
            description="meinchat demo top-up",
        )
        print(f"  alice +{gap} → {target_balance}")
    else:
        print(f"  alice already at {current} (target {target_balance})")
    db.session.commit()

    print("── 5. conversation + token-transfer system message ──────")
    conv_service = ConversationService(repo=ConversationRepository(db.session))
    message_service = MessageService(
        conv_repo=ConversationRepository(db.session),
        message_repo=MessageRepository(db.session),
        nickname_repo=NicknameRepository(db.session),
    )
    conv = conv_service.start_or_get(alice.id, bob.id)
    # Idempotency: only post the demo text once. The plugin doesn't
    # have a "find by body" repo method, so we use a marker prefix
    # and skip if any message in the convo already starts with it.
    marker = "[demo-meinchat] "
    existing_msgs = MessageRepository(db.session).page(conv.id, limit=200)
    if not any(m.body.startswith(marker) for m in existing_msgs):
        message_service.send_text(
            conv.id,
            sender_user_id=alice.id,
            body=f"{marker}hi bobby — sending you 25 tokens for lunch.",
        )
        print("  posted demo greeting")
    else:
        print("  demo greeting already in conversation")
    db.session.commit()

    print("── 6. demo bot-widget (cms, optional) ────────────────────")
    _seed_demo_widget(db)

    print()
    print(f"  conversation id: {conv.id}")
    print("  done.")


def main() -> int:
    """Self-bootstrapping entrypoint for the platform installer, which invokes
    ``PYTHONPATH=. python plugins/meinchat/populate_db.py --force``. Creates the
    app + pushes a context, then runs the in-context ``populate_db`` body. Extra
    argv (e.g. ``--force``) is intentionally ignored — the seed is always a full
    idempotent upsert."""
    app = _ensure_app_context()
    with app.app_context():
        populate_db()
    return 0


# The shipped widget envelope is the SINGLE SOURCE of the demo bot-widget's
# slug + config (DRY) — no hardcoded dict lives here anymore. The `assistant`
# BOT is provisioned by bot_meinchat's populate_db.py — run that first so the
# member resolves; widget-start still 404s gracefully if the bot is absent.
_WIDGET_JSON_RELATIVE_PATH = "docs/import/cms/widgets/meinchat-bot-widget.json"
_CMS_WIDGETS_ENTITY_KEY = "cms_widgets"


def _seed_demo_widget(db) -> None:
    """Idempotently seed the public MeinchatChatWidget by importing the shipped
    JSON envelope through the unified ``cms_widgets`` data-exchange mechanism
    (the JSON is the single source of truth — same path as
    ``flask data-exchange import cms_widgets <file>``).

    cms is a SOFT dependency: if the ``cms_widgets`` exchanger is not registered
    (cms absent/disabled) or the cms import code is unavailable, skip cleanly —
    meinchat only soft-depends on cms (S86.3 D2)."""
    import json
    from pathlib import Path

    from vbwd.services.data_exchange.registry import data_exchange_registry

    exchanger = data_exchange_registry.get(_CMS_WIDGETS_ENTITY_KEY)
    if exchanger is None:
        print("  cms not present — skipping demo widget")
        return

    try:
        from vbwd.services.data_exchange.envelope import validate_envelope
        from vbwd.services.data_exchange.port import MODE_UPSERT
    except ImportError:
        print("  cms not present — skipping demo widget")
        return

    widget_json_path = Path(__file__).resolve().parent / _WIDGET_JSON_RELATIVE_PATH
    with open(widget_json_path, encoding="utf-8") as handle:
        payload = json.load(handle)

    # Fail fast on a malformed envelope (same guard the CLI applies); returns
    # the rows so we can name the seeded slug in the log without re-hardcoding it.
    rows = validate_envelope(payload, _CMS_WIDGETS_ENTITY_KEY)

    try:
        result = exchanger.import_(payload, mode=MODE_UPSERT, dry_run=False)
    except ImportError:
        print("  cms not present — skipping demo widget")
        return

    db.session.commit()
    slug = rows[0]["slug"] if rows else _CMS_WIDGETS_ENTITY_KEY
    print(
        f"  seeded public widget '{slug}' via cms_widgets import "
        f"(created={result.created}, updated={result.updated})"
    )


if __name__ == "__main__":
    sys.exit(main())
