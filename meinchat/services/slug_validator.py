"""Nickname slug validator — one source of truth for what a nickname can be."""
import re


class NicknameInvalidError(ValueError):
    """Raised when a nickname fails validation."""


_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")

_RESERVED = frozenset(
    {
        "admin",
        "api",
        "me",
        "null",
        "root",
        "support",
        "system",
        "vbwd",
    }
)


def validate_nickname(nickname: str) -> None:
    """Raise NicknameInvalidError if the nickname is not acceptable.

    Rules:
      * length 3–32, starts with a lowercase letter
      * characters: lowercase ASCII letters, digits, '_' or '-'
      * no consecutive dashes (readable, avoids homoglyph tricks)
      * not in the reserved list
    """
    if not isinstance(nickname, str):
        raise NicknameInvalidError("must be a string")
    # Reserved check runs before length so e.g. "me" fails with the clearer
    # reserved-word message rather than a length error.
    if nickname in _RESERVED:
        raise NicknameInvalidError(f"'{nickname}' is reserved")
    if len(nickname) < 3 or len(nickname) > 32:
        raise NicknameInvalidError("length must be 3–32 characters")
    if not _PATTERN.match(nickname):
        raise NicknameInvalidError(
            "must match ^[a-z][a-z0-9_-]{2,31}$ — lowercase letters, "
            "digits, '_' or '-'; no leading digit/dash/underscore; ASCII only"
        )
    if "--" in nickname:
        raise NicknameInvalidError("consecutive dashes are not allowed")
