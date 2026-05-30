# meinchat — backend plugin bundle

User identity (nickname directory) + address book + 1-on-1 messaging
(text + image attachments + SSE) + peer-to-peer token transfer.

## Subsystems

| Subsystem | Routes (prefix `/api/v1/`) |
|---|---|
| Nickname | `nickname/me`, `nickname/search`, `nickname/<n>/card` |
| Contacts | `contacts`, `contacts/<id>` |
| Messaging | `messaging/conversations`, `messaging/conversations/<id>/messages`, `messaging/conversations/<id>/messages/attachment`, `messaging/conversations/<id>/read`, `messaging/conversations/<id>/messages/<mid>` |
| SSE | `messaging/stream`, `messaging/stream/token` |
| Token transfer | `token-transfer`, `token-transfer/history` |
| Admin moderation | `admin/meinchat/nicknames`, `admin/meinchat/nicknames/<id>/{ban,unban}`, `admin/meinchat/transfers` (no conversation-content inspector — privacy) |

## Architecture

- `meinchat/__init__.py` — `MeinchatPlugin(BasePlugin)` class.
- `meinchat/meinchat/models/` — 5 SQLAlchemy models (`UserNickname`,
  `UserContact`, `Conversation`, `Message`, `TokenTransferRecord`).
- `meinchat/meinchat/repositories/` — 5 repos.
- `meinchat/meinchat/services/` — slug validator, nickname, contact,
  conversation, message, attachment, rate-limiter, event-bus,
  redis-event-bus, stream-token, token-transfer.
- `meinchat/meinchat/routes.py` — flat Blueprint, all endpoints with
  absolute paths (CSRF-exempt at the plugin-mount layer).
- `migrations/versions/` — 4 Alembic ops, idempotent.
- `populate_db.py` — service-driven idempotent demo seed.

## SSE design

`POST /messaging/stream/token` mints a 60-min JWT (`aud=meinchat-stream`).
Browser opens `EventSource('/messaging/stream?stream_token=<jwt>')`.
Backend uses Redis pub/sub when available so fan-out crosses gunicorn
workers; falls back to an in-process bus when Redis is unreachable.

## Locked decisions (sprint 57)

- Free nickname change, no cooldown.
- Token transfer minimum = 1 (positive integer).
- Attachment hard-deleted with parent message.
- Hard message-delete on both sides (no tombstone).
- Banned-nickname slug freed after `nickname_ban_grace_period_days`
  (config, default 30).
- Per-user private contacts.
- No live presence — last seen derived from `last_message_at`.

## Tests

```
docker compose run --rm test python -m pytest plugins/meinchat/tests/ -v
docker compose --profile test-integration run --rm test-integration \
    bash -c "pytest plugins/meinchat/tests/integration/ -v"
```

## Demo data

```
docker compose exec api python /app/plugins/meinchat/populate_db.py
```

Idempotent: re-running prints "already" everywhere; no duplicate rows.
