"""Admin guest-token management (top-up / reset existing widget guests).

The D11 token economy grants ``guest_initial_tokens`` ONLY at first-provision of
a widget guest (``widget_start_service._grant_initial_token_budget``). A
returning guest is never re-granted, so once a guest's GLOBAL core balance hits
0 the pre-send gate (``widget_room_meter.guard_send``, balance ``<= 0``)
402-gates them and raising the admin ``guest_initial_tokens`` setting does
nothing for the existing guest. This service gives an admin a way to top-up or
reset an existing guest's balance (and bulk variants for "give everyone N more"
/ "reset everyone to N").

Balances are the single source of truth in core's ``TokenService`` — this
service routes every credit/debit through it (DRY) with
``TokenTransactionType.ADJUSTMENT`` for a clear admin audit trail. The guest's
identity / existence is read from the meinchat ``GuestSessionRepository``;
collaborators arrive via the constructor (DI) so unit tests pass MagicMocks.
"""
from typing import Any, Callable, Dict, List, Optional
from uuid import UUID

from vbwd.models.enums import TokenTransactionType, UserRole


_TOPUP_DESCRIPTION = "meinchat admin guest token top-up"
_RESET_DESCRIPTION = "meinchat admin guest token reset"


class GuestNotFoundError(Exception):
    """The target id is not an existing widget GUEST (route → 404)."""


class GuestTokenAdminService:
    """Top-up / reset the core token balance of an existing widget guest."""

    def __init__(
        self,
        *,
        token_service: Any,
        guest_session_repo: Any,
        resolve_user_role: Callable[[UUID], Optional[UserRole]],
        nickname_repo: Any,
    ) -> None:
        self._token_service = token_service
        self._guest_session_repo = guest_session_repo
        self._resolve_user_role = resolve_user_role
        self._nickname_repo = nickname_repo

    # ── single-guest operations ─────────────────────────────────────────────
    def topup(self, guest_user_id: UUID, amount: int) -> int:
        """Credit ``amount`` tokens to the guest. Returns the new balance.

        Raises ``ValueError`` for a non-positive amount and ``GuestNotFoundError``
        when the id is not an existing widget guest.
        """
        self._require_existing_guest(guest_user_id)
        positive_amount = self._require_positive_amount(amount)
        self._token_service.credit_tokens(
            guest_user_id,
            positive_amount,
            transaction_type=TokenTransactionType.ADJUSTMENT,
            description=_TOPUP_DESCRIPTION,
        )
        return self._token_service.get_balance(guest_user_id)

    def reset(self, guest_user_id: UUID, target: int) -> int:
        """Set the guest's balance to exactly ``target``. Returns the new balance.

        Computes the signed delta from the current balance and credits / debits
        the absolute difference (a no-op when already at target). Raises
        ``ValueError`` for a negative target and ``GuestNotFoundError`` for an
        unknown guest.
        """
        self._require_existing_guest(guest_user_id)
        non_negative_target = self._require_non_negative_target(target)
        current_balance = self._token_service.get_balance(guest_user_id)
        delta = non_negative_target - current_balance
        if delta > 0:
            self._token_service.credit_tokens(
                guest_user_id,
                delta,
                transaction_type=TokenTransactionType.ADJUSTMENT,
                description=_RESET_DESCRIPTION,
            )
        elif delta < 0:
            self._token_service.debit_tokens(
                guest_user_id,
                -delta,
                transaction_type=TokenTransactionType.ADJUSTMENT,
                description=_RESET_DESCRIPTION,
            )
        return self._token_service.get_balance(guest_user_id)

    # ── bulk operations ─────────────────────────────────────────────────────
    def topup_all(self, amount: int) -> int:
        """Top-up every distinct guest by ``amount``. Returns the count affected."""
        positive_amount = self._require_positive_amount(amount)
        affected = 0
        for guest_user_id in self._guest_session_repo.all_guest_user_ids():
            self.topup(guest_user_id, positive_amount)
            affected += 1
        return affected

    def reset_all(self, target: int) -> int:
        """Reset every distinct guest to ``target``. Returns the count affected."""
        non_negative_target = self._require_non_negative_target(target)
        affected = 0
        for guest_user_id in self._guest_session_repo.all_guest_user_ids():
            self.reset(guest_user_id, non_negative_target)
            affected += 1
        return affected

    # ── listing ─────────────────────────────────────────────────────────────
    def list_guests(
        self, *, page: int, per_page: int, query: Optional[str] = None
    ) -> Dict[str, Any]:
        """Paged, distinct-by-``guest_user_id`` listing enriched with the live
        core balance and a display name (nickname, else session display_name)."""
        result = self._guest_session_repo.list_distinct_guests(
            page=page, per_page=per_page, query=query
        )
        items: List[Dict[str, Any]] = [self._to_item(row) for row in result["items"]]
        return {
            "items": items,
            "page": page,
            "per_page": per_page,
            "total": result["total"],
        }

    # ── internals ───────────────────────────────────────────────────────────
    def _to_item(self, row: Any) -> Dict[str, Any]:
        guest_user_id = row.guest_user_id
        display_name = self._display_name_for(row)
        last_seen = row.updated_at or row.created_at
        return {
            "guest_user_id": str(guest_user_id),
            "display_name": display_name,
            "widget_slug": row.widget_slug,
            "balance": self._token_service.get_balance(guest_user_id),
            "last_seen": last_seen.isoformat() if last_seen else None,
        }

    def _display_name_for(self, row: Any) -> Optional[str]:
        nickname = self._nickname_repo.find_by_user_id(row.guest_user_id)
        if nickname is not None and getattr(nickname, "nickname", None):
            return nickname.nickname
        return row.display_name

    def _require_existing_guest(self, guest_user_id: UUID) -> None:
        session = self._guest_session_repo.find_by_guest_user_id(guest_user_id)
        if session is None:
            raise GuestNotFoundError(f"no widget guest for id {guest_user_id}")
        if self._resolve_user_role(guest_user_id) != UserRole.GUEST:
            raise GuestNotFoundError(f"user {guest_user_id} is not a guest")

    @staticmethod
    def _require_positive_amount(amount: int) -> int:
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError("amount must be a positive integer")
        return amount

    @staticmethod
    def _require_non_negative_target(target: int) -> int:
        if not isinstance(target, int) or isinstance(target, bool) or target < 0:
            raise ValueError("target must be a non-negative integer")
        return target
