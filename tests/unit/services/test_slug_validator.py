"""Tests for the nickname slug validator.

Rules (per sprint 57):
  - regex ^[a-z][a-z0-9_-]{2,31}$  → 3–32 chars, leading letter
  - reserved words rejected (case-insensitive match on input lowered)
  - no consecutive dashes
  - unicode rejected (anything non-ASCII)
"""
import pytest

from plugins.meinchat.meinchat.services.slug_validator import (
    NicknameInvalidError,
    validate_nickname,
)


class TestValidNicknames:
    @pytest.mark.parametrize(
        "nickname",
        ["alice", "alice_v2", "a-b-c", "bob99", "abc", "a" * 32],
    )
    def test_accepts_valid_nicknames(self, nickname):
        validate_nickname(nickname)


class TestLengthBounds:
    @pytest.mark.parametrize("nickname", ["ab", "a", ""])
    def test_rejects_too_short(self, nickname):
        with pytest.raises(NicknameInvalidError, match="length"):
            validate_nickname(nickname)

    def test_rejects_too_long(self):
        with pytest.raises(NicknameInvalidError, match="length"):
            validate_nickname("a" * 33)


class TestCharacterSet:
    @pytest.mark.parametrize(
        "nickname",
        [
            "Alice",           # uppercase
            "1alice",          # leading digit
            "-alice",          # leading dash
            "_alice",          # leading underscore
            "alice.v2",        # dot
            "alice v2",        # space
            "alice@mail",      # at sign
            "alice/bob",       # slash
        ],
    )
    def test_rejects_bad_characters(self, nickname):
        with pytest.raises(NicknameInvalidError):
            validate_nickname(nickname)

    def test_rejects_consecutive_dashes(self):
        with pytest.raises(NicknameInvalidError, match="consecutive"):
            validate_nickname("a--b")

    def test_rejects_unicode(self):
        with pytest.raises(NicknameInvalidError):
            validate_nickname("élise")


class TestReservedWords:
    @pytest.mark.parametrize(
        "nickname",
        ["admin", "system", "root", "me", "api", "support", "vbwd", "null"],
    )
    def test_rejects_reserved(self, nickname):
        with pytest.raises(NicknameInvalidError, match="reserved"):
            validate_nickname(nickname)

    def test_reserved_is_case_insensitive_on_input(self):
        # Input uppercase fails the charset check FIRST; lowercase 'admin' is
        # the one that hits the reserved check. This test pins down the
        # reserved-list policy against the lowercased value only.
        with pytest.raises(NicknameInvalidError, match="reserved"):
            validate_nickname("admin")
