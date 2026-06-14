# Meinchat Bot Widget — Developer Guide

The bot-widget (sprint **S86.3**) is a **CMS `vue-component` widget** that, on
"Start Conversation", auto-creates a **meinchat room** between the visitor and a
configured set of members (any of which may be bots) and renders the chat inline
on the page. It builds on **S86.1 rooms** (multi-party conversations) and the
**S86 bot bridge** (`bot_base` + `bot_meinchat`).

Audience: engineers integrating, extending, or operating the feature.

---

## Architecture at a glance

```
CMS page (layout area)
  └─ CmsWidgetRenderer  (widget_type == "vue-component")
       └─ MeinchatChatWidget.vue        (fe-user/plugins/meinchat)   ← registered via registerCmsVueComponent
            │  reads widget.config (member_nicknames, visibility, …)
            ▼
   POST /api/v1/messaging/widget/start  (backend/plugins/meinchat)
        ├─ ICmsWidgetReader  → reads the widget's config by slug (SERVER-TRUSTED; soft dep on cms)
        ├─ GUEST provisioning (public)  → UserRole.GUEST + nickname + short JWT
        └─ RoomService.create_room(creator, member_user_ids, widget_slug=…)   ← S86.1
   ───────────────────────────────────────────────────────────────────
   Then the normal room endpoints:
        POST /api/v1/messaging/rooms/<id>/messages   (guest question)
        bot_meinchat inbound hook fires on the room send → assistant replies INTO the room
        WidgetRoomChargeHook debits tokens per word (D11)
```

Key principle: the widget's behaviour-defining config (**who is invited**,
**public vs logged-in**) is read **server-side from the stored widget**, never
trusted from the request body — otherwise an anonymous client could invite
arbitrary users.

---

## Frontend

- **`vbwd-fe-user/plugins/meinchat/src/components/MeinchatChatWidget.vue`** —
  registered in the plugin `index.ts` via a dynamic import:
  `registerCmsVueComponent('MeinchatChatWidget', module.default)` (soft dep on
  the cms registry, the same pattern booking/shop use). `CmsWidgetRenderer`
  passes `:config="{...widget.config, widget_slug: widget.slug}"`.
  - Renders: permission gate → name/nickname prompt → Start button → chat pane.
  - **Guest-scoped auth:** on a `public` widget, `widget/start` returns an
    `access_token` (a GUEST JWT). All subsequent room calls + the SSE stream use
    **that token** (an explicit `authToken` override on the api helpers), NOT the
    app session. The token is persisted in a **cookie + long-lived localStorage**
    keyed by `widget_slug` and presented back to `widget/start` on return so the
    same guest + balance is reused (no re-grant — **D12**).
  - Chat pane composes the existing `MessageBubble` + `MessageComposer` (not the
    dashboard `RoomView`, which is app-session/E2E-coupled) + the room stream;
    applies `applyBotConversationStyle` (S70.4).
- **fe-admin editor:** `vbwd-fe-admin/plugins/meinchat-admin/src/widgets/MeinchatChatWidgetEditorTab.vue`,
  registered through the **shared widget-editor seam** re-exported from
  `cms-admin` (`registerWidgetEditor` / `getWidgetEditor` /
  `VueWidgetEditorDescriptor`). `CmsWidgetEditor.vue` stays widget-agnostic.

### Widget config schema (`cms_widget.config`)
```jsonc
{
  "component_name": "MeinchatChatWidget",
  "member_nicknames": ["assistant"],   // who the visitor is connected to (bots or people)
  "visibility": "public",              // "public" | "logged_in"
  "title": "Chat with us",
  "welcome_message": "…",
  "composer_placeholder": "Type your message…",
  "start_button_label": "Start Conversation",
  "display": "inline",                 // "inline" | "dock"
  "open_by_default": true
}
```

---

## Backend

### `POST /api/v1/messaging/widget/start`  (no `@require_auth`)
Body `{ widget_slug, display_name? }`, optional `Authorization: Bearer <guest_jwt>` for return visits.

- Resolves the widget config via **`ICmsWidgetReader`** (a narrow port; a
  `NullCmsWidgetReader` default returns `None` → `404 widget_not_found` when cms
  is absent — meinchat does **not** hard-depend on cms).
- Reads `member_nicknames` + `visibility` from the stored config (server-trusted).
- `visibility == "logged_in"`: requires a valid bearer (`401` if absent); a
  caller without a nickname → `409 {code:"nickname_required"}`.
- `visibility == "public"`: requires `display_name` (`400`); **all configured
  members must be `UserRole.BOT`** else `409 public_human_member_not_allowed`;
  IP-rate-limited (`widget_guest_start`); provisions a **GUEST** user
  (`GuestSessionService`, mirrors `BotSenderProvisioner`) + nickname + a short
  JWT (`AuthService.generate_access_token`); records a `meinchat_guest_session`.
- Creates/returns the room: `{ room_id, self_nickname, members[], access_token?, token_balance? }`.

### The room is a normal S86.1 room
- `meinchat_room` + `meinchat_room_member` (creator = `admin`, configured members
  = `member`). Messages reuse `meinchat_message` via `room_id`.
- **Protocol:** any BOT member ⇒ `plain` (bots can't decrypt E2E). Human-only
  rooms can be `e2e_v1` (S86.2) — not relevant to bot-widget rooms.
- **Bot replies:** `bot_meinchat`'s inbound hook fires on a room send when a bot
  user is a member; the reply posts back into the room. Routing (S86.3 D7):
  `/command` → that command's namespace; free text → the room's active owner
  (`BotSession`), else the help menu (the LLM-bot fallback is a follow-on).

### Token economy (D11 — 1 token = 1 word)
- On a fresh public start the guest is granted `guest_initial_tokens` (core
  `TokenService.credit_tokens`, idempotent; not re-granted on D12 reuse).
- **Gate:** a guest send in a widget room is allowed only while balance `> 0`;
  otherwise `402 {code:"insufficient_tokens"}` (no message, bot not triggered).
- **Charge:** a post-send hook (`WidgetRoomChargeHook`) debits
  `word_count × guest_token_cost_per_word` from the room's guest for **both** the
  guest's question and the bot's answer (the bot reply is also a room send).
- `GET /api/v1/messaging/widget/balance` (authed) returns the live balance for
  the FE buy-block.
- **Config resolution gotcha:** `_meinchat_config()` returns only persisted
  overrides; the economy keys fall back to **`DEFAULT_CONFIG`** via
  `_economy_config()` (do not read them with literal `0`/`True` defaults).

### Config keys (`config.json` / `DEFAULT_CONFIG` / `admin-config.json`)
| Key | Default | Meaning |
|---|---|---|
| `guest_economy_enabled` | `true` | Master switch for grant + metering |
| `guest_initial_tokens` | `20` | Grant per fresh guest |
| `guest_token_cost_per_word` | `1` | Tokens per word |
| `widget_guest_token_ttl_hours` | (see config) | Guest JWT TTL |
| `rate_widget_guest_start_*` | (see config) | IP rate limit on `widget/start` |

---

## Seeding the widget — the import JSON + unified CLI

The widget ships as a **data-exchange envelope** (the single source of truth):

```
plugins/meinchat/docs/import/cms/widgets/meinchat-bot-widget.json
```
```json
{ "vbwd_export": "cms_widgets", "version": 1, "cms_widgets": [ { "slug": "meinchat-bot-widget", "widget_type": "vue-component", "content_json": {"component": "MeinchatChatWidget"}, "config": { … }, "is_active": true } ] }
```

Import it through the **unified data-exchange CLI** (upsert by slug):

```bash
# inside the api container (factory app target):
flask --app "vbwd.app:create_app" data-exchange import cms_widgets \
  /app/plugins/meinchat/docs/import/cms/widgets/meinchat-bot-widget.json
# add --dry-run to preview, --mode replace_all to drop-then-import
```

The **meinchat populate** (`plugins/meinchat/populate_db.py`, `_seed_demo_widget`)
imports this same JSON through the registered `cms_widgets` exchanger, so a
normal install seeds it. **Order:** run `bot_meinchat`'s populate first — it
**eagerly provisions the `assistant` BOT** the widget references (otherwise
`widget/start` 404s on the unknown member).

> Installer note: ensure `bot_meinchat` and `meinchat` are in the install
> recipe's populate list (and expose a `populate_db()` entrypoint) so the
> assistant + widget are seeded out-of-box.

---

## Extension points

- **A real conversational LLM bot:** implement a `BotCommandProvider`
  (`bot_namespace`, `get_bot_commands`, `handle_action`) that calls your LLM, and
  make it the free-text default owner. Today free text falls back to the bot help
  menu.
- **Multiple distinct bot members:** the room-encoded `ChatRef` already isolates
  sessions per room; give each bot user its own inbound-hook instance
  (`is_bot_in_room` + `resolve_bot_user_id`) and select which bot reacts. No
  `bot_base` change needed.
- **A real `/tokens` purchase page:** wire the buy-block target (the widget reads
  `config.buy_tokens_href`, default `/tokens`).

## Test map
- Backend: `plugins/meinchat/tests/**` (widget-start, room meter, economy config
  fallback), `plugins/bot_meinchat/tests/**` (room round-trip, eager populate).
- Frontend: `vbwd-fe-user/plugins/meinchat/tests/unit/components/meinchat-chat-widget.spec.ts`,
  `vbwd-fe-admin/plugins/meinchat-admin/tests/unit/meinchatChatWidgetEditor.spec.ts`.
- Quality gate: `bin/pre-commit-check.sh --plugin meinchat --full` (+ `bot_meinchat`).

## See also
- Specs: `docs/dev_log/20260613/sprints/s86-bot-widget.md` (umbrella) + `s86-1/2/3`.
- Report: `docs/dev_log/20260613/reports/11-s86-rooms-and-bot-widget.md`.
- Walkthrough: `docs/dev_log/20260613/walkthrough/s86-WALK-REPORT-bot-widget.html`.
