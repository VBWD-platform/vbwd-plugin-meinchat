"""S70.0 — structured-message `meta` on MessageService.send_text.

A bot reply can attach a nullable structured `meta` JSON (rendered as choice
cards by the fe) that rides alongside the plain `body`. Liskov: a send with
`meta=None` (the default) is byte-for-byte the legacy behaviour — these specs
pin both the additive path and the validation that keeps `meta` safe + bounded.
`action_data` is opaque (never parsed/trusted here).
"""
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from plugins.meinchat.meinchat.services.message_service import MessageService


def _conversation(user_a, user_b):
    from plugins.meinchat.meinchat.models.conversation import Conversation
    from plugins.meinchat.meinchat.services.conversation_service import order_pair

    low, high = order_pair(user_a, user_b)
    conv = Conversation()
    conv.id = uuid4()
    conv.participant_low_id = low
    conv.participant_high_id = high
    conv.unread_low_count = 0
    conv.unread_high_count = 0
    return conv


def _nickname_row(user_id, nickname):
    from plugins.meinchat.meinchat.models.user_nickname import UserNickname

    row = UserNickname()
    row.user_id = user_id
    row.nickname = nickname
    return row


@pytest.fixture
def conv_repo():
    repo = MagicMock()
    repo.save.side_effect = lambda row: row
    return repo


@pytest.fixture
def message_repo():
    repo = MagicMock()
    repo.save.side_effect = lambda row: row
    return repo


@pytest.fixture
def nickname_repo():
    return MagicMock()


@pytest.fixture
def service(conv_repo, message_repo, nickname_repo):
    return MessageService(
        conv_repo=conv_repo, message_repo=message_repo, nickname_repo=nickname_repo
    )


@pytest.fixture
def seeded_conversation(conv_repo, nickname_repo):
    alice, bob = uuid4(), uuid4()
    conv = _conversation(alice, bob)
    conv_repo.find_by_id.return_value = conv
    nickname_repo.find_by_user_id.return_value = _nickname_row(alice, "alice")
    return conv, alice


class TestMetaPersistence:
    def test_default_meta_is_none_legacy_behaviour_unchanged(
        self, service, seeded_conversation
    ):
        conv, alice = seeded_conversation
        msg = service.send_text(conv.id, sender_user_id=alice, body="hi")
        assert msg.meta is None
        assert msg.to_dict()["meta"] is None

    def test_bot_choices_meta_is_persisted_and_serialized(
        self, service, seeded_conversation
    ):
        conv, alice = seeded_conversation
        meta = {
            "kind": "bot_choices",
            "choices": [
                {"label": "Pro", "action_data": "subscription:plan:42", "hint": "€29"},
                {"label": "Cancel", "action_data": "bot-base:cancel:0"},
            ],
        }
        msg = service.send_text(
            conv.id, sender_user_id=alice, body="Pick one", meta=meta
        )
        assert msg.meta == meta
        assert msg.to_dict()["meta"] == meta

    def test_bot_action_meta_is_persisted(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        meta = {"kind": "bot_action", "action_data": "subscription:plan:42"}
        msg = service.send_text(conv.id, sender_user_id=alice, body="Pro", meta=meta)
        assert msg.meta == meta


class TestMetaValidation:
    def test_rejects_non_dict_meta(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        with pytest.raises(ValueError):
            service.send_text(
                conv.id, sender_user_id=alice, body="x", meta=["not", "a", "dict"]
            )

    def test_rejects_oversize_meta(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        huge = {"kind": "bot_action", "action_data": "x" * 9000}
        with pytest.raises(ValueError):
            service.send_text(conv.id, sender_user_id=alice, body="x", meta=huge)

    def test_rejects_bot_choices_without_choices_list(
        self, service, seeded_conversation
    ):
        conv, alice = seeded_conversation
        with pytest.raises(ValueError):
            service.send_text(
                conv.id,
                sender_user_id=alice,
                body="x",
                meta={"kind": "bot_choices", "choices": "nope"},
            )

    def test_rejects_choice_missing_label_or_action_data(
        self, service, seeded_conversation
    ):
        conv, alice = seeded_conversation
        with pytest.raises(ValueError):
            service.send_text(
                conv.id,
                sender_user_id=alice,
                body="x",
                meta={"kind": "bot_choices", "choices": [{"label": "Pro"}]},
            )

    def test_rejects_choice_non_string_action_data(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        with pytest.raises(ValueError):
            service.send_text(
                conv.id,
                sender_user_id=alice,
                body="x",
                meta={
                    "kind": "bot_choices",
                    "choices": [{"label": "Pro", "action_data": 42}],
                },
            )

    def test_rejects_bot_action_without_action_data(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        with pytest.raises(ValueError):
            service.send_text(
                conv.id, sender_user_id=alice, body="x", meta={"kind": "bot_action"}
            )

    def test_accepts_optional_hint(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        meta = {
            "kind": "bot_choices",
            "choices": [{"label": "Pro", "action_data": "a:b:c", "hint": "fast"}],
        }
        msg = service.send_text(conv.id, sender_user_id=alice, body="x", meta=meta)
        assert msg.meta == meta

    def test_rejects_non_string_hint(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        with pytest.raises(ValueError):
            service.send_text(
                conv.id,
                sender_user_id=alice,
                body="x",
                meta={
                    "kind": "bot_choices",
                    "choices": [{"label": "Pro", "action_data": "a:b:c", "hint": 9}],
                },
            )

    def test_unknown_kind_meta_is_allowed_opaque(self, service, seeded_conversation):
        """An unknown `kind` carries no shape contract — stored as-is so future
        client-only kinds need no backend change (additive, size-capped)."""
        conv, alice = seeded_conversation
        meta = {"kind": "future_thing", "anything": [1, 2, 3]}
        msg = service.send_text(conv.id, sender_user_id=alice, body="x", meta=meta)
        assert msg.meta == meta


class TestBotChoicesTextValidation:
    """S70.3 — a ``bot_choices`` meta may carry an optional clean prompt
    ``text`` (shown instead of the numbered body on card clients)."""

    def test_accepts_optional_text(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        meta = {
            "kind": "bot_choices",
            "text": "Choose a plan:",
            "choices": [{"label": "Pro", "action_data": "a:b:c"}],
        }
        msg = service.send_text(conv.id, sender_user_id=alice, body="x", meta=meta)
        assert msg.meta == meta

    def test_rejects_non_string_text(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        with pytest.raises(ValueError):
            service.send_text(
                conv.id,
                sender_user_id=alice,
                body="x",
                meta={
                    "kind": "bot_choices",
                    "text": 7,
                    "choices": [{"label": "Pro", "action_data": "a:b:c"}],
                },
            )


class TestBotMenuValidation:
    """S70.3 — ``bot_menu`` lists command rows ``{command, description}``."""

    def test_accepts_valid_bot_menu(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        meta = {
            "kind": "bot_menu",
            "commands": [
                {"command": "/tarifs", "description": "Browse tarif plans"},
                {"command": "/help", "description": "show this menu"},
            ],
        }
        msg = service.send_text(conv.id, sender_user_id=alice, body="x", meta=meta)
        assert msg.meta == meta

    def test_rejects_bot_menu_without_commands_list(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        with pytest.raises(ValueError):
            service.send_text(
                conv.id,
                sender_user_id=alice,
                body="x",
                meta={"kind": "bot_menu", "commands": "nope"},
            )

    def test_rejects_command_row_missing_command(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        with pytest.raises(ValueError):
            service.send_text(
                conv.id,
                sender_user_id=alice,
                body="x",
                meta={
                    "kind": "bot_menu",
                    "commands": [{"description": "no command field"}],
                },
            )

    def test_rejects_command_row_non_string_description(
        self, service, seeded_conversation
    ):
        conv, alice = seeded_conversation
        with pytest.raises(ValueError):
            service.send_text(
                conv.id,
                sender_user_id=alice,
                body="x",
                meta={
                    "kind": "bot_menu",
                    "commands": [{"command": "/x", "description": 9}],
                },
            )


class TestBotCartValidation:
    """S70.3 — ``bot_cart`` carries priced line items + a total + currency."""

    def _valid_cart(self):
        return {
            "kind": "bot_cart",
            "items": [
                {
                    "name": "Pro",
                    "quantity": 1,
                    "unit_price": "29.00",
                    "line_total": "29.00",
                }
            ],
            "total": "29.00",
            "currency": "EUR",
        }

    def test_accepts_valid_bot_cart(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        meta = self._valid_cart()
        msg = service.send_text(conv.id, sender_user_id=alice, body="x", meta=meta)
        assert msg.meta == meta

    def test_accepts_empty_cart(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        meta = {"kind": "bot_cart", "items": [], "total": 0, "currency": "EUR"}
        msg = service.send_text(conv.id, sender_user_id=alice, body="x", meta=meta)
        assert msg.meta == meta

    def test_rejects_items_not_a_list(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        bad = self._valid_cart()
        bad["items"] = "nope"
        with pytest.raises(ValueError):
            service.send_text(conv.id, sender_user_id=alice, body="x", meta=bad)

    def test_rejects_item_missing_name(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        bad = self._valid_cart()
        del bad["items"][0]["name"]
        with pytest.raises(ValueError):
            service.send_text(conv.id, sender_user_id=alice, body="x", meta=bad)

    def test_rejects_non_integer_quantity(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        bad = self._valid_cart()
        bad["items"][0]["quantity"] = "two"
        with pytest.raises(ValueError):
            service.send_text(conv.id, sender_user_id=alice, body="x", meta=bad)

    def test_rejects_missing_total(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        bad = self._valid_cart()
        del bad["total"]
        with pytest.raises(ValueError):
            service.send_text(conv.id, sender_user_id=alice, body="x", meta=bad)

    def test_rejects_non_string_currency(self, service, seeded_conversation):
        conv, alice = seeded_conversation
        bad = self._valid_cart()
        bad["currency"] = 9
        with pytest.raises(ValueError):
            service.send_text(conv.id, sender_user_id=alice, body="x", meta=bad)
