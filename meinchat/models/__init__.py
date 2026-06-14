"""Meinchat models — import all to register with SQLAlchemy."""
from plugins.meinchat.meinchat.models.user_nickname import UserNickname  # noqa: F401
from plugins.meinchat.meinchat.models.user_contact import UserContact  # noqa: F401
from plugins.meinchat.meinchat.models.conversation import Conversation  # noqa: F401
from plugins.meinchat.meinchat.models.room import Room  # noqa: F401
from plugins.meinchat.meinchat.models.room_member import RoomMember  # noqa: F401
from plugins.meinchat.meinchat.models.guest_session import (  # noqa: F401
    MeinchatGuestSession,
)
from plugins.meinchat.meinchat.models.message import Message  # noqa: F401
from plugins.meinchat.meinchat.models.attachment import (  # noqa: F401
    MeinchatAttachment,
)
import plugins.meinchat.meinchat.models.token_transfer  # noqa: F401
