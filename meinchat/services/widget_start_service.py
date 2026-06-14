"""Orchestrate ``POST /messaging/widget/start`` (S86.3, D2/D4/D5).

The bot-widget becomes a room auto-created for the visitor. This service is the
single home for the start flow's business rules; the route is a thin adapter
that maps the typed errors below onto HTTP status codes.

Server-trusted config (member nicknames + visibility) comes from the cms widget
row via ``ICmsWidgetReader`` — never from the request body (AD10). Branching:

- ``visibility == 'logged_in'``: the caller must be authenticated and own a
  nickname; the configured members are invited to a room the caller admins.
- ``visibility == 'public'``: a ``display_name`` is required; D4 requires every
  configured member to be a ``UserRole.BOT``; a fresh GUEST is provisioned, the
  members invited, and the guest session row persisted.

Token economy (D11) and session reuse / overuse protection (D12), S86.3 3c:

- A NEW public start grants ``guest_initial_tokens`` to the freshly provisioned
  guest via the injected ``grant_initial_tokens`` callable (wired to the core
  ``TokenService`` in the route). The grant is idempotent against simple reloads
  because a *reused* guest (D12) is never re-provisioned and never re-granted.
- When the caller presents a valid guest credential that resolves to an existing
  guest session for THIS widget, the start reuses that guest's room instead of
  provisioning a fresh guest (D12).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, List, Optional
from uuid import UUID

from vbwd.models.enums import UserRole
from vbwd.utils.datetime_utils import utcnow_aware

from plugins.meinchat.meinchat.models.guest_session import MeinchatGuestSession
from plugins.meinchat.meinchat.models.room_member import ROOM_ROLE_ADMIN

_DEFAULT_VISIBILITY = "logged_in"
_VISIBILITY_PUBLIC = "public"


class WidgetNotFoundError(Exception):
    """No active MeinchatChatWidget for the slug (cms absent → also this)."""


class WidgetAuthRequiredError(Exception):
    """A ``logged_in`` widget was started without a valid bearer token."""


class NicknameRequiredError(Exception):
    """A ``logged_in`` caller has no meinchat nickname yet."""


class DisplayNameRequiredError(Exception):
    """A ``public`` widget start omitted the required ``display_name``."""


class PublicHumanMemberError(Exception):
    """D4 — a public widget configured a non-BOT human member."""


class UnknownMemberError(Exception):
    """A configured member nickname does not resolve to a user."""


@dataclass(frozen=True)
class WidgetStartResult:
    """What the route serialises back to the client."""

    room_id: UUID
    self_nickname: Optional[str]
    members: List[dict]
    access_token: Optional[str] = None
    # Set on the public (guest) path — fresh or reused — so the route can surface
    # the guest's remaining token balance for the FE buy block (D11). None on the
    # logged_in path.
    guest_user_id: Optional[UUID] = None


class WidgetStartService:
    def __init__(
        self,
        *,
        widget_reader,
        resolve_nickname_to_user_id: Callable[[str], Optional[UUID]],
        resolve_user_role: Callable[[UUID], Optional[UserRole]],
        resolve_user_nickname: Callable[[UUID], Optional[str]],
        room_service,
        guest_session_service,
        guest_session_repo,
        guest_session_ttl_hours: float,
        grant_initial_tokens: Optional[Callable[[UUID], None]] = None,
        guest_initial_tokens: int = 0,
        economy_enabled: bool = False,
        mint_guest_access_token: Optional[Callable[[UUID], Optional[str]]] = None,
    ) -> None:
        self._widget_reader = widget_reader
        self._resolve_nickname_to_user_id = resolve_nickname_to_user_id
        self._resolve_user_role = resolve_user_role
        self._resolve_user_nickname = resolve_user_nickname
        self._room_service = room_service
        self._guest_session_service = guest_session_service
        self._guest_session_repo = guest_session_repo
        self._guest_session_ttl_hours = guest_session_ttl_hours
        self._grant_initial_tokens = grant_initial_tokens
        self._guest_initial_tokens = guest_initial_tokens
        self._economy_enabled = economy_enabled
        self._mint_guest_access_token = mint_guest_access_token

    def start(
        self,
        widget_slug: str,
        *,
        display_name: Optional[str],
        caller_user_id: Optional[UUID],
        presented_guest_user_id: Optional[UUID] = None,
    ) -> WidgetStartResult:
        config = self._widget_reader.get_active_widget_config(widget_slug)
        if config is None:
            raise WidgetNotFoundError(widget_slug)

        member_nicknames = config.get("member_nicknames") or []
        visibility = config.get("visibility") or _DEFAULT_VISIBILITY

        if visibility == _VISIBILITY_PUBLIC:
            return self._start_public(
                widget_slug, member_nicknames, display_name, presented_guest_user_id
            )
        return self._start_logged_in(widget_slug, member_nicknames, caller_user_id)

    # ── logged_in branch ─────────────────────────────────────────────────────

    def _start_logged_in(
        self,
        widget_slug: str,
        member_nicknames: List[str],
        caller_user_id: Optional[UUID],
    ) -> WidgetStartResult:
        if caller_user_id is None:
            raise WidgetAuthRequiredError(widget_slug)
        caller_nickname = self._resolve_user_nickname(caller_user_id)
        if not caller_nickname:
            raise NicknameRequiredError(str(caller_user_id))

        member_ids = self._resolve_members(member_nicknames)
        room = self._room_service.create_room(
            caller_user_id, member_ids, widget_slug=widget_slug
        )
        return WidgetStartResult(
            room_id=room.id,
            self_nickname=caller_nickname,
            members=self._member_dtos(room.id),
        )

    # ── public branch ────────────────────────────────────────────────────────

    def _start_public(
        self,
        widget_slug: str,
        member_nicknames: List[str],
        display_name: Optional[str],
        presented_guest_user_id: Optional[UUID],
    ) -> WidgetStartResult:
        reused = self._reuse_guest_session(widget_slug, presented_guest_user_id)
        if reused is not None:
            return reused

        if not display_name or not display_name.strip():
            raise DisplayNameRequiredError(widget_slug)

        member_ids = self._resolve_members(member_nicknames)
        self._reject_human_members(member_ids)

        guest = self._guest_session_service.provision(display_name.strip())
        room = self._room_service.create_room(
            guest.user_id, member_ids, widget_slug=widget_slug
        )
        self._persist_guest_session(guest, widget_slug, room.id, display_name.strip())
        # D11 — grant the initial token budget ONLY to a freshly provisioned
        # guest. A reused guest (handled above) is never re-granted, so a refresh
        # or return does not mint free tokens.
        self._grant_initial_token_budget(guest.user_id)
        return WidgetStartResult(
            room_id=room.id,
            self_nickname=guest.nickname,
            members=self._member_dtos(room.id),
            access_token=guest.access_token,
            guest_user_id=guest.user_id,
        )

    def _reuse_guest_session(
        self, widget_slug: str, presented_guest_user_id: Optional[UUID]
    ) -> Optional[WidgetStartResult]:
        """D12 — return the existing room for a valid returning guest.

        A presented guest credential is honoured only when it resolves to a
        ``GUEST`` user that already owns a session for THIS widget. Returns
        ``None`` (→ fresh provision) when no such session exists, so an
        invalid/absent credential degrades to the normal new-guest path."""
        if presented_guest_user_id is None:
            return None
        if self._resolve_user_role(presented_guest_user_id) != UserRole.GUEST:
            return None
        session = self._guest_session_repo.find_for_widget(
            presented_guest_user_id, widget_slug
        )
        if session is None or session.room_id is None:
            return None
        # D12 — a returning guest is never re-provisioned and never re-granted,
        # but it MUST still receive a usable access token so the FE keeps driving
        # the guest's room as the guest (never falling back to the app session).
        # The grant stays idempotent because re-minting a token mints no tokens.
        access_token = (
            self._mint_guest_access_token(presented_guest_user_id)
            if self._mint_guest_access_token is not None
            else None
        )
        return WidgetStartResult(
            room_id=session.room_id,
            self_nickname=self._resolve_user_nickname(presented_guest_user_id),
            members=self._member_dtos(session.room_id),
            access_token=access_token,
            guest_user_id=presented_guest_user_id,
        )

    def _grant_initial_token_budget(self, guest_user_id: UUID) -> None:
        if (
            not self._economy_enabled
            or self._grant_initial_tokens is None
            or self._guest_initial_tokens <= 0
        ):
            return
        self._grant_initial_tokens(guest_user_id)

    # ── shared helpers ───────────────────────────────────────────────────────

    def _resolve_members(self, member_nicknames: List[str]) -> List[UUID]:
        resolved: List[UUID] = []
        for nickname in member_nicknames:
            user_id = self._resolve_nickname_to_user_id(nickname)
            if user_id is None:
                raise UnknownMemberError(nickname)
            resolved.append(user_id)
        return resolved

    def _reject_human_members(self, member_ids: List[UUID]) -> None:
        for user_id in member_ids:
            if self._resolve_user_role(user_id) != UserRole.BOT:
                raise PublicHumanMemberError(str(user_id))

    def _member_dtos(self, room_id: UUID) -> List[dict]:
        dtos = []
        for member in self._room_service.members(room_id):
            dtos.append(
                {
                    "nickname": self._resolve_user_nickname(member.user_id),
                    "role": member.role,
                    "is_admin": member.role == ROOM_ROLE_ADMIN,
                }
            )
        return dtos

    def _persist_guest_session(
        self, guest, widget_slug: str, room_id: UUID, display_name: str
    ) -> MeinchatGuestSession:
        row = MeinchatGuestSession()
        row.guest_user_id = guest.user_id
        row.widget_slug = widget_slug
        row.room_id = room_id
        row.display_name = display_name
        row.expires_at = utcnow_aware() + timedelta(hours=self._guest_session_ttl_hours)
        return self._guest_session_repo.save(row)
