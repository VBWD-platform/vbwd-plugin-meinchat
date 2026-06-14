"""Word-based guest token economy for the public bot-widget room (REVISED D11).

**1 token = 1 word.** The economy has two cohesive responsibilities:

1. **Pre-send gate** (``WidgetRoomMeter.guard_send``) — a guest in a widget room
   with the economy on may send only while balance ``> 0``. A send at balance
   ``<= 0`` raises ``InsufficientGuestTokensError`` so the room-message route
   returns ``402`` BEFORE creating the message / triggering the bot. The gate
   never pre-charges; the in-flight round may spend balance to (or below) zero —
   the NEXT send is gated. A non-guest sender / non-widget room / economy-off is
   never gated.

2. **Post-send per-word charge** (``WidgetRoomChargeHook`` — an ``IPostSendHook``)
   — after ANY message in a widget room with the economy on, debit
   ``word_count(body) × cost_per_word`` from the room's GUEST member (the member
   whose user role is ``GUEST``). This fires for BOTH the guest's question and
   the bot's answer, so the guest "pays for questions and answers, 1 token/word".
   A body-less / meta-only message = 0 words = no debit. The debit clamps at the
   guest's balance (the core ``TokenService.debit_tokens`` raises on an overdraw,
   so we never ask for more than the balance) and NEVER raises — the message is
   already sent.

Balances live in the core ``TokenService`` (no meinchat-owned ledger).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional
from uuid import UUID

from vbwd.models.enums import TokenTransactionType, UserRole

logger = logging.getLogger(__name__)

_QUESTION_DESCRIPTION = "meinchat widget message (per-word)"


def word_count(body: Optional[str]) -> int:
    """Number of whitespace-separated words in ``body`` (the unit of billing).

    A ``None`` / empty / whitespace-only body counts as zero words."""
    return len((body or "").split())


class InsufficientGuestTokensError(Exception):
    """The guest's balance is not positive — the next send is gated (D11)."""


class WidgetRoomMeter:
    """The pre-send balance gate for guest sends in widget rooms (D11)."""

    def __init__(
        self,
        *,
        token_service,
        resolve_user_role: Callable[[UUID], Optional[UserRole]],
        economy_enabled: bool,
    ) -> None:
        self._token_service = token_service
        self._resolve_user_role = resolve_user_role
        self._economy_enabled = economy_enabled

    def guard_send(self, room, sender_user_id: UUID) -> None:
        """Raise ``InsufficientGuestTokensError`` when a metered guest's balance
        is not positive; otherwise a no-op (the send is allowed). Never charges."""
        if not self._applies(room, sender_user_id):
            return
        if self._token_service.get_balance(sender_user_id) <= 0:
            raise InsufficientGuestTokensError(
                "balance exhausted — buy tokens to continue"
            )

    def _applies(self, room, sender_user_id: UUID) -> bool:
        if not self._economy_enabled:
            return False
        if room is None or not getattr(room, "widget_slug", None):
            return False
        return self._resolve_user_role(sender_user_id) == UserRole.GUEST


class WidgetRoomChargeHook:
    """Post-send per-word charge (``IPostSendHook``).

    Charges the room's GUEST member ``word_count × cost_per_word`` for ANY message
    in a widget room (guest question OR bot answer). Collaborators arrive as
    providers so each call builds them off the current request session / config
    (the hook is registered once at plugin-enable time).
    """

    def __init__(
        self,
        *,
        token_service_provider: Callable[[], Any],
        room_provider: Callable[[UUID], Any],
        members_provider: Callable[[UUID], List[Any]],
        resolve_user_role: Callable[[UUID], Optional[UserRole]],
        economy_enabled_provider: Callable[[], bool],
        cost_per_word_provider: Callable[[], int],
    ) -> None:
        self._token_service_provider = token_service_provider
        self._room_provider = room_provider
        self._members_provider = members_provider
        self._resolve_user_role = resolve_user_role
        self._economy_enabled_provider = economy_enabled_provider
        self._cost_per_word_provider = cost_per_word_provider

    def on_sent(self, row, *, fetched_by=None) -> None:
        """Debit the room's guest for this message's words. Never raises — a sent
        message must not be undone by a metering failure."""
        try:
            self._charge(row)
        except Exception as exc:  # pragma: no cover - defensive, hooks never fail
            logger.error("widget per-word charge failed: %s", exc)

    def _charge(self, row) -> None:
        if not self._economy_enabled_provider():
            return
        room_id = getattr(row, "room_id", None)
        if room_id is None:
            return  # a 1:1 message — nothing to meter
        room = self._room_provider(room_id)
        if room is None or not getattr(room, "widget_slug", None):
            return
        words = word_count(getattr(row, "body", None))
        if words <= 0:
            return
        cost_per_word = self._cost_per_word_provider()
        if cost_per_word <= 0:
            return
        guest_user_id = self._guest_member_id(room_id)
        if guest_user_id is None:
            return

        token_service = self._token_service_provider()
        balance = token_service.get_balance(guest_user_id)
        if balance <= 0:
            return
        amount = min(words * cost_per_word, balance)
        if amount <= 0:
            return
        token_service.debit_tokens(
            guest_user_id,
            amount,
            TokenTransactionType.USAGE,
            description=_QUESTION_DESCRIPTION,
        )

    def _guest_member_id(self, room_id: UUID) -> Optional[UUID]:
        for member in self._members_provider(room_id):
            if self._resolve_user_role(member.user_id) == UserRole.GUEST:
                return member.user_id
        return None
