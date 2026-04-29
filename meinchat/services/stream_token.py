"""Short-lived JWT used as the SSE connection's auth credential.

`EventSource` can't set request headers, so the access token can't ride
in `Authorization`. Instead the client mints a separate token with a
narrow audience (`meinchat-stream`) and passes it as a query parameter.
"""
import time
from typing import Any

import jwt as pyjwt


_AUDIENCE = "meinchat-stream"
_ALGO = "HS256"


class StreamTokenInvalidError(Exception):
    pass


class StreamTokenExpiredError(Exception):
    pass


class StreamTokenService:
    def __init__(self, secret_key: str, ttl_seconds: int = 3600) -> None:
        self._secret = secret_key
        self._ttl = ttl_seconds

    def mint(self, user_id: Any) -> str:
        now = int(time.time())
        payload = {
            "user_id": str(user_id),
            "aud": _AUDIENCE,
            "iat": now,
            "exp": now + self._ttl,
        }
        return pyjwt.encode(payload, self._secret, algorithm=_ALGO)

    def verify(self, token: str) -> str:
        try:
            payload = pyjwt.decode(
                token,
                self._secret,
                algorithms=[_ALGO],
                audience=_AUDIENCE,
            )
        except pyjwt.ExpiredSignatureError as exc:
            raise StreamTokenExpiredError("stream token expired") from exc
        except pyjwt.PyJWTError as exc:
            raise StreamTokenInvalidError(str(exc)) from exc

        user_id = payload.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise StreamTokenInvalidError("user_id claim missing")
        return user_id
