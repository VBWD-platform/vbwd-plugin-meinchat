"""Unit tests for the word-based guest token economy (S86.3, REVISED D11).

1 token = 1 word. The economy has two responsibilities, kept cohesive in
``widget_room_meter``:

- ``WidgetRoomMeter.guard_send(room, sender_user_id)`` — the PRE-send gate. A
  guest in a widget room with economy on may send only while balance > 0; a send
  at balance <= 0 raises ``InsufficientGuestTokensError`` so the route returns
  402 BEFORE creating the message / triggering the bot. Non-guest sender /
  non-widget room / economy-off → no gate.

- ``WidgetRoomChargeHook.on_sent(row)`` — the POST-send per-word charge. For ANY
  message sent in a widget room with economy on, debit
  ``word_count(body) × cost_per_word`` from the room's GUEST member (the member
  whose user role is ``GUEST``). This fires for BOTH the guest's question and the
  bot's answer (both charged to the guest). A body-less / meta-only message =
  0 words = no debit. The debit clamps at the guest's balance (never overdraws,
  never raises — the message is already sent), so a 5-word answer over a 2-token
  balance leaves the guest at 0.

All collaborators are injected fakes — no DB, clean DI.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from vbwd.models.enums import UserRole

from plugins.meinchat.meinchat.services.widget_room_meter import (
    InsufficientGuestTokensError,
    WidgetRoomChargeHook,
    WidgetRoomMeter,
    word_count,
)


class _FakeTokenService:
    def __init__(self, balances=None):
        self._balances = dict(balances or {})
        self.debits = []

    def get_balance(self, user_id):
        return self._balances.get(user_id, 0)

    def debit_tokens(self, user_id, amount, transaction_type, **kwargs):
        # Mirrors the core TokenService contract: RAISES when the balance can't
        # cover the debit (the hook must clamp BEFORE calling so it never trips).
        current = self._balances.get(user_id, 0)
        if amount <= 0:
            raise ValueError("Debit amount must be positive")
        if current < amount:
            raise ValueError("Insufficient token balance")
        self._balances[user_id] = current - amount
        self.debits.append((user_id, amount))
        return SimpleNamespace(balance=self._balances[user_id])


def _widget_room(widget_slug="w"):
    return SimpleNamespace(id=uuid4(), widget_slug=widget_slug)


# ── word_count ───────────────────────────────────────────────────────────────


def test_word_count_counts_whitespace_separated_tokens():
    assert word_count("one two three") == 3
    assert word_count("  spaced   out  words ") == 3
    assert word_count("single") == 1


def test_word_count_of_empty_or_none_is_zero():
    assert word_count("") == 0
    assert word_count("   ") == 0
    assert word_count(None) == 0


# ── gate (pre-send) ──────────────────────────────────────────────────────────


def _meter(token_service, *, enabled=True, roles=None):
    roles = roles or {}
    return WidgetRoomMeter(
        token_service=token_service,
        resolve_user_role=lambda uid: roles.get(uid),
        economy_enabled=enabled,
    )


def test_gate_allows_guest_with_positive_balance():
    guest_id = uuid4()
    token_service = _FakeTokenService({guest_id: 1})
    meter = _meter(token_service, roles={guest_id: UserRole.GUEST})

    # No raise → the send is allowed; the gate never pre-charges.
    meter.guard_send(_widget_room(), guest_id)
    assert token_service.debits == []


def test_gate_blocks_guest_at_zero_balance():
    guest_id = uuid4()
    token_service = _FakeTokenService({guest_id: 0})
    meter = _meter(token_service, roles={guest_id: UserRole.GUEST})

    with pytest.raises(InsufficientGuestTokensError):
        meter.guard_send(_widget_room(), guest_id)


def test_gate_blocks_guest_with_negative_balance():
    guest_id = uuid4()
    token_service = _FakeTokenService({guest_id: -3})
    meter = _meter(token_service, roles={guest_id: UserRole.GUEST})

    with pytest.raises(InsufficientGuestTokensError):
        meter.guard_send(_widget_room(), guest_id)


def test_gate_does_not_block_logged_in_sender():
    user_id = uuid4()
    token_service = _FakeTokenService({user_id: 0})
    meter = _meter(token_service, roles={user_id: UserRole.USER})

    meter.guard_send(_widget_room(), user_id)  # no raise


def test_gate_does_not_block_in_non_widget_room():
    guest_id = uuid4()
    token_service = _FakeTokenService({guest_id: 0})
    meter = _meter(token_service, roles={guest_id: UserRole.GUEST})

    meter.guard_send(_widget_room(widget_slug=None), guest_id)  # no raise


def test_gate_does_not_block_when_economy_disabled():
    guest_id = uuid4()
    token_service = _FakeTokenService({guest_id: 0})
    meter = _meter(token_service, enabled=False, roles={guest_id: UserRole.GUEST})

    meter.guard_send(_widget_room(), guest_id)  # no raise


# ── post-send charge hook ────────────────────────────────────────────────────


def _room_with_guest(guest_id, widget_slug="w"):
    room = SimpleNamespace(id=uuid4(), widget_slug=widget_slug)
    members = [
        SimpleNamespace(user_id=guest_id),
        SimpleNamespace(user_id=uuid4()),  # a bot member
    ]
    return room, members


def _hook(
    token_service,
    room,
    members,
    *,
    enabled=True,
    cost_per_word=1,
    charge_bot_answers=True,
    roles=None,
):
    roles = roles or {}
    return WidgetRoomChargeHook(
        token_service_provider=lambda: token_service,
        room_provider=lambda room_id: room if room_id == room.id else None,
        members_provider=lambda room_id: members if room_id == room.id else [],
        resolve_user_role=lambda uid: roles.get(uid),
        economy_enabled_provider=lambda: enabled,
        cost_per_word_provider=lambda: cost_per_word,
        charge_bot_answers_provider=lambda: charge_bot_answers,
    )


def _message(room, body, *, sender_user_id=None):
    # The production Message row exposes its author as `sender_id` (the model
    # column), so the fake must too — the charge hook reads `sender_id`.
    return SimpleNamespace(room_id=room.id, body=body, sender_id=sender_user_id)


def test_charges_guest_for_a_guest_question_by_word():
    guest_id = uuid4()
    room, members = _room_with_guest(guest_id)
    bot_id = members[1].user_id
    token_service = _FakeTokenService({guest_id: 10})
    hook = _hook(
        token_service,
        room,
        members,
        roles={guest_id: UserRole.GUEST, bot_id: UserRole.BOT},
    )

    hook.on_sent(_message(room, "how much is shipping"))  # 4 words

    assert token_service.debits == [(guest_id, 4)]
    assert token_service.get_balance(guest_id) == 6


def test_charges_guest_for_a_bot_answer_by_word():
    guest_id = uuid4()
    room, members = _room_with_guest(guest_id)
    bot_id = members[1].user_id
    token_service = _FakeTokenService({guest_id: 10})
    hook = _hook(
        token_service,
        room,
        members,
        roles={guest_id: UserRole.GUEST, bot_id: UserRole.BOT},
    )

    # The BOT sent this message, but the GUEST member is charged for it.
    hook.on_sent(SimpleNamespace(room_id=room.id, body="shipping is five euros"))  # 4

    assert token_service.debits == [(guest_id, 4)]


def test_charge_clamps_at_guest_balance_and_never_raises():
    guest_id = uuid4()
    room, members = _room_with_guest(guest_id)
    bot_id = members[1].user_id
    token_service = _FakeTokenService({guest_id: 2})
    hook = _hook(
        token_service,
        room,
        members,
        roles={guest_id: UserRole.GUEST, bot_id: UserRole.BOT},
    )

    # A 5-word answer over a 2-token balance → clamp to 2, never raise.
    hook.on_sent(_message(room, "one two three four five"))

    assert token_service.debits == [(guest_id, 2)]
    assert token_service.get_balance(guest_id) == 0


def test_zero_word_message_is_a_no_op():
    guest_id = uuid4()
    room, members = _room_with_guest(guest_id)
    token_service = _FakeTokenService({guest_id: 10})
    hook = _hook(token_service, room, members, roles={guest_id: UserRole.GUEST})

    hook.on_sent(_message(room, ""))  # meta-only / body-less
    hook.on_sent(_message(room, None))

    assert token_service.debits == []
    assert token_service.get_balance(guest_id) == 10


def test_no_op_when_room_has_no_guest_member():
    room, members = _room_with_guest(uuid4())
    members[0] = SimpleNamespace(user_id=uuid4())  # replace guest with a non-guest
    token_service = _FakeTokenService()
    hook = _hook(token_service, room, members, roles={})

    hook.on_sent(_message(room, "hello there friend"))

    assert token_service.debits == []


def test_no_op_when_economy_disabled():
    guest_id = uuid4()
    room, members = _room_with_guest(guest_id)
    token_service = _FakeTokenService({guest_id: 10})
    hook = _hook(
        token_service,
        room,
        members,
        enabled=False,
        roles={guest_id: UserRole.GUEST},
    )

    hook.on_sent(_message(room, "hello there friend"))

    assert token_service.debits == []


def test_no_op_for_a_non_widget_room():
    guest_id = uuid4()
    room, members = _room_with_guest(guest_id, widget_slug=None)
    token_service = _FakeTokenService({guest_id: 10})
    hook = _hook(token_service, room, members, roles={guest_id: UserRole.GUEST})

    hook.on_sent(_message(room, "hello there friend"))

    assert token_service.debits == []


def test_no_op_for_a_one_to_one_message_with_no_room():
    token_service = _FakeTokenService()
    room = SimpleNamespace(id=uuid4(), widget_slug="w")
    hook = _hook(token_service, room, [], roles={})

    # A 1:1 message has room_id == None — nothing to charge.
    hook.on_sent(SimpleNamespace(room_id=None, body="hi there"))

    assert token_service.debits == []


# ── guest_charge_bot_answers toggle (default true preserves today's behavior) ─


def test_bot_answer_still_charged_when_charge_bot_answers_default_true():
    """Default (charge_bot_answers=True) preserves TODAY's behavior exactly: a
    BOT-authored message in a widget room still debits the guest per word."""
    guest_id = uuid4()
    room, members = _room_with_guest(guest_id)
    bot_id = members[1].user_id
    token_service = _FakeTokenService({guest_id: 10})
    hook = _hook(
        token_service,
        room,
        members,
        charge_bot_answers=True,
        roles={guest_id: UserRole.GUEST, bot_id: UserRole.BOT},
    )

    # Authored by the bot, not the guest.
    hook.on_sent(_message(room, "shipping is five euros", sender_user_id=bot_id))

    assert token_service.debits == [(guest_id, 4)]


def test_bot_answer_free_when_charge_bot_answers_false():
    """charge_bot_answers=False: a message NOT authored by the guest (the bot's
    answer) is free — the guest is not debited."""
    guest_id = uuid4()
    room, members = _room_with_guest(guest_id)
    bot_id = members[1].user_id
    token_service = _FakeTokenService({guest_id: 10})
    hook = _hook(
        token_service,
        room,
        members,
        charge_bot_answers=False,
        roles={guest_id: UserRole.GUEST, bot_id: UserRole.BOT},
    )

    hook.on_sent(_message(room, "shipping is five euros", sender_user_id=bot_id))

    assert token_service.debits == []
    assert token_service.get_balance(guest_id) == 10


def test_guest_own_words_still_charged_when_charge_bot_answers_false():
    """charge_bot_answers=False: the guest still pays for the words THEY authored
    (a message whose sender is the guest member)."""
    guest_id = uuid4()
    room, members = _room_with_guest(guest_id)
    bot_id = members[1].user_id
    token_service = _FakeTokenService({guest_id: 10})
    hook = _hook(
        token_service,
        room,
        members,
        charge_bot_answers=False,
        roles={guest_id: UserRole.GUEST, bot_id: UserRole.BOT},
    )

    hook.on_sent(_message(room, "how much is shipping", sender_user_id=guest_id))  # 4

    assert token_service.debits == [(guest_id, 4)]
    assert token_service.get_balance(guest_id) == 6
