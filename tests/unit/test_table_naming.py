"""Oracle: meinchat tables must be `meinchat_`-prefixed (sprint S43).

Bare names (`token_transfer`, `user_contact`, …) collide-risk across plugins and
hide ownership. This guards the S43.0a renames against regression.
"""
import pytest

from plugins.meinchat.meinchat.models.token_transfer import TokenTransferRecord
from plugins.meinchat.meinchat.models.user_contact import UserContact
from plugins.meinchat.meinchat.models.user_nickname import UserNickname


@pytest.mark.parametrize(
    "model, expected",
    [
        (TokenTransferRecord, "meinchat_token_transfer"),
        (UserContact, "meinchat_user_contact"),
        (UserNickname, "meinchat_user_nickname"),
    ],
)
def test_meinchat_table_is_plugin_prefixed(model, expected):
    assert model.__tablename__ == expected
    assert model.__tablename__.startswith("meinchat_")
