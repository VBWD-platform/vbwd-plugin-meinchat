"""Integration: NicknameRepository against a real Postgres + the
generated `nickname_ci` column + the `text_pattern_ops` prefix index.

Service-layer tests mock the repo, so SQL correctness — case-folding,
prefix matching, ordering — is unverified there. These specs run
against the real DB to confirm the storage layer behaves as the service
assumes.
"""
from __future__ import annotations

import pytest


@pytest.mark.integration
def test_prefix_search_is_case_insensitive_and_excludes_caller(app):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    from plugins.meinchat.meinchat.repositories.nickname_repository import (
        NicknameRepository,
    )
    from plugins.meinchat.meinchat.services.nickname_service import (
        NicknameService,
    )

    with app.app_context():
        user_repo = UserRepository(db.session)
        auth_service = app.container.auth_service()

        # Three users with deterministic nicknames covering the cases
        # we care about: prefix match, case-fold match, caller exclusion.
        seeds = [
            ("alice-repo@example.com", "alice-repo"),
            ("alex-repo@example.com", "alex-repo"),
            ("bob-repo@example.com", "bob-repo"),
        ]
        nick_service = NicknameService(repo=NicknameRepository(db.session))
        ids: dict[str, str] = {}
        for email, nick in seeds:
            existing = user_repo.find_by_email(email)
            if existing is None:
                auth_service.register(email=email, password="RepoTest123@")
                existing = user_repo.find_by_email(email)
            ids[nick] = existing.id
            if nick_service.get_mine(existing.id) is None:
                nick_service.set_nickname(existing.id, nick)
        db.session.commit()

        repo = NicknameRepository(db.session)

        # Case-folded prefix: "AL" should match alice-repo + alex-repo.
        results = repo.search_prefix("AL")
        names = {r.nickname for r in results}
        assert "alice-repo" in names
        assert "alex-repo" in names
        assert "bob-repo" not in names

        # Caller exclusion: when the search comes from alex-repo, alex's
        # own row drops out of the result set (used by the typeahead so
        # admin doesn't see themselves in their own contact picker).
        results_excluding_self = repo.search_prefix(
            "al", exclude_user_id=ids["alex-repo"]
        )
        excluded_names = {r.nickname for r in results_excluding_self}
        assert "alex-repo" not in excluded_names
        assert "alice-repo" in excluded_names

        # `limit` argument is honoured (we cap at 10 in production).
        capped = repo.search_prefix("a", limit=1)
        assert len(capped) <= 1


@pytest.mark.integration
def test_provisioned_bot_user_is_findable_in_nickname_search(app):
    """A BOT-role user provisioned through BotSenderProvisioner appears in the
    user-facing nickname search, so any user can find it and start a chat.

    This is the findability guarantee the bot-meinchat refinement relies on:
    nickname search filters only on banned / search_hidden, NOT on role, so a
    provisioned (un-banned, visible) BOT user is found like any other peer — no
    meinchat-side change is needed."""
    from vbwd.extensions import db
    from vbwd.models.enums import UserRole
    from vbwd.repositories.user_repository import UserRepository

    from plugins.meinchat.meinchat.repositories.nickname_repository import (
        NicknameRepository,
    )
    from plugins.meinchat.meinchat.services.bot_sender_provisioner import (
        BotSenderProvisioner,
    )
    from plugins.meinchat.meinchat.services.nickname_service import NicknameService

    with app.app_context():
        session = db.session
        user_repo = UserRepository(session)
        provisioner = BotSenderProvisioner(
            user_service=app.container.user_service(),
            user_repository=user_repo,
            nickname_service=NicknameService(NicknameRepository(session)),
            session=session,
        )

        bot_user_id = provisioner.ensure_bot_sender(
            "findable-bot@bot.local", "assistantbot"
        )
        db.session.commit()

        # The provisioned account really is a BOT.
        assert user_repo.find_by_id(bot_user_id).role == UserRole.BOT

        # And it shows up in the user-facing prefix search a human would run.
        repo = NicknameRepository(session)
        hits = {row.nickname for row in repo.search_prefix("assistant")}
        assert "assistantbot" in hits


@pytest.mark.integration
def test_paged_listing_returns_correct_total(app):
    from vbwd.extensions import db

    from plugins.meinchat.meinchat.repositories.nickname_repository import (
        NicknameRepository,
    )

    with app.app_context():
        repo = NicknameRepository(db.session)
        result = repo.list_paged(page=1, per_page=5)
        # The seeded users from previous specs guarantee at least 3 rows.
        assert result["total"] >= 3
        assert result["page"] == 1
        assert result["per_page"] == 5
        assert len(result["items"]) <= 5

        # Filter narrows the total too (not just the page slice).
        narrow = repo.list_paged(page=1, per_page=50, query="bob-repo")
        assert narrow["total"] >= 1
        assert all(r.nickname.startswith("bob-repo") for r in narrow["items"])
