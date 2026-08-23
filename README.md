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

### ⚠ Reverse-proxy requirement — DO NOT buffer the SSE stream

`GET /api/v1/messaging/stream` is a long-lived `text/event-stream`. The handler
sets `X-Accel-Buffering: no`, but **every proxy hop must disable buffering** or
the browser receives nothing until the connection closes — messages appear
"only on refresh" (iOS uses polling, so it is *not* affected, which makes this
look like an iOS-only / backend bug when it is actually a proxy-buffering one).
Symptom check: `curl -N '.../messaging/stream?stream_token=…'` returns 0 bytes /
`time_starttransfer=0` instead of an immediate `: connected` comment.

nginx **consumes** the upstream `X-Accel-Buffering` header and does NOT forward
it, so each hop must be configured (or re-assert it). For a typical
browser → front proxy (e.g. HestiaCP) → instance nginx → gunicorn chain:

```nginx
location = /api/v1/messaging/stream {
    proxy_pass        <upstream>;
    proxy_http_version 1.1;
    proxy_set_header  Connection "";
    proxy_buffering   off;
    proxy_cache       off;
    proxy_read_timeout 3600s;
    add_header X-Accel-Buffering no always;   # re-assert for the next hop
}
```

This block ships in `vbwd-fe-user/nginx.{dev,prod}.conf*` (instance hop) **and**
the front proxy templates in `vbwd-demo-instances/{setup.sh,fix-nginx-templates.sh}`
(Hestia hop). Both hops are required. *(Root cause of the 2026-05-30 "messages
not pushed to the browser on vbwd.cc" incident — the Hestia front proxy buffered
the stream.)*

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

## Documentation

Full platform documentation lives at **[vbwd.cc/docs](https://vbwd.cc/docs)**.

- [Plugin system](https://vbwd.cc/docs-plugin-system) — how backend plugins are registered, enabled, and configured
- [Chat / meinchat](https://vbwd.cc/docs-core-meinchat) — documentation for this plugin's domain
- [Architecture](https://vbwd.cc/docs-architecture) — platform layering and the core-agnosticism rule
- [Getting started](https://vbwd.cc/docs-getting-started) — install a VBWD instance and enable plugins
