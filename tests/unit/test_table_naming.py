"""Oracle: every meinchat table is `meinchat_`-prefixed (sprint S43.0).

Bare names (`message`, `conversation`, `token_transfer`, …) collide-risk across
plugins and hide ownership. Guards the S43.0a + S43.0b renames against
regression.
"""
import pytest

from plugins.meinchat.meinchat.models.conversation import Conversation
from plugins.meinchat.meinchat.models.message import Message
from plugins.meinchat.meinchat.models.attachment import MeinchatAttachment
from plugins.meinchat.meinchat.models.token_transfer import TokenTransferRecord
from plugins.meinchat.meinchat.models.user_contact import UserContact
from plugins.meinchat.meinchat.models.user_nickname import UserNickname

_MODELS = [
    Conversation,
    Message,
    MeinchatAttachment,
    TokenTransferRecord,
    UserContact,
    UserNickname,
]


@pytest.mark.parametrize("model", _MODELS)
def test_meinchat_table_is_plugin_prefixed(model):
    assert model.__tablename__.startswith("meinchat_"), model.__tablename__


def test_specific_meinchat_table_names():
    assert Conversation.__tablename__ == "meinchat_conversation"
    assert Message.__tablename__ == "meinchat_message"
    assert TokenTransferRecord.__tablename__ == "meinchat_token_transfer"
    assert UserContact.__tablename__ == "meinchat_user_contact"
    assert UserNickname.__tablename__ == "meinchat_user_nickname"
