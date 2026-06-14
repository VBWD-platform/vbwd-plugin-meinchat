# Meinchat Bot Widget — LLM Coding-Agent Reference

Dense, structured reference for an AI coding agent modifying or extending the
bot-widget. Authoritative facts, exact paths, contracts, invariants, gotchas.
Verify against source before acting; do not guess. Plugin code under
`vbwd-backend/plugins/` is gitignored (on-disk only).

## Identity
- Feature: a CMS `vue-component` widget that creates a meinchat **room** on
  "Start Conversation" and chats with configured members (bots/people).
- Sprints: **S86.1** rooms + core `UserRole.GUEST`; **S86.2a** room E2E (N/A to
  widget — widget rooms are `plain`); **S86.3** the widget; D11 token economy
  (REVISED: 1 token = 1 word); D12 level-1 overuse protection.

## File map (exact)
```
backend (plugins/meinchat/meinchat/):
  routes.py                          widget/start, /rooms/*, /widget/balance; _meinchat_config(), _economy_config()
  services/widget_start_service.py   WidgetStartService (public/logged_in branches, grant, D12 reuse)
  services/guest_session_service.py  GuestSessionService.provision() → GUEST user + nickname + JWT
  services/widget_room_meter.py      WidgetRoomMeter (gate guard_send + per-word charge)
  services/room_service.py           RoomService.create_room/invite/...
  extensibility/cms_widget_reader.py ICmsWidgetReader, NullCmsWidgetReader, CmsWidgetReader (soft dep)
  models/room.py, room_member.py, guest_session.py, message.py (room_id)
plugins/meinchat/__init__.py         DEFAULT_CONFIG; on_enable DI + WidgetRoomChargeHook registration
plugins/meinchat/populate_db.py      _seed_demo_widget() imports the JSON via cms_widgets exchanger
plugins/meinchat/docs/import/cms/widgets/meinchat-bot-widget.json   widget source-of-truth (envelope)
plugins/bot_meinchat/__init__.py     _resolve_bot_user_id, _is_bot_in_room, MeinchatInboundHook wiring
plugins/bot_meinchat/bot_meinchat/services/meinchat_provider.py     ChatRef "room:<id>" encode/decode
plugins/bot_meinchat/populate_db.py  eager assistant provisioning
core (vbwd/):
  models/enums.py                    UserRole.GUEST
  services/auth_service.py           generate_access_token(); login guard rejects BOT+GUEST
  services/token_service.py          get_balance / credit_tokens / debit_tokens
  cli/data_exchange.py               flask data-exchange import|export|list
frontend:
  vbwd-fe-user/plugins/meinchat/src/components/MeinchatChatWidget.vue
  vbwd-fe-user/plugins/meinchat/src/widget/{widgetApi.ts,guestTokenStore.ts}
  vbwd-fe-user/plugins/meinchat/index.ts   registerCmsVueComponent('MeinchatChatWidget', …)
  vbwd-fe-admin/plugins/meinchat-admin/src/widgets/MeinchatChatWidgetEditorTab.vue
  vbwd-fe-admin/plugins/cms-admin/index.ts re-exports registerWidgetEditor/getWidgetEditor/VueWidgetEditorDescriptor
```

## API contract
- `POST /api/v1/messaging/widget/start` — NO `@require_auth`. Body `{widget_slug, display_name?}`; optional `Authorization: Bearer <guest_jwt>` (D12 reuse).
  - `404 widget_not_found` (unknown/inactive widget OR cms absent → null reader).
  - `logged_in`: no bearer → `401`; caller without nickname → `409 {code:"nickname_required"}`.
  - `public`: missing `display_name` → `400`; a configured non-BOT member → `409 public_human_member_not_allowed`; unknown member nickname → `404`; rate-limited `widget_guest_start`.
  - `200` → `{room_id, self_nickname, members:[{nickname,role,is_admin}], access_token?(public), token_balance?(economy on)}`.
- Room ops (auth = app session OR guest JWT): `GET /messaging/rooms/<id>`, `GET|POST /messaging/rooms/<id>/messages`, `POST /messaging/rooms/<id>/read`, `GET /messaging/rooms/<id>/members`, `POST /messaging/rooms/<id>/invite` `{nickname}`, `POST /messaging/rooms/<id>/leave`, `DELETE /messaging/rooms/<id>/members/<user_id>`.
  - Guest message send when balance ≤ 0 → `402 {code:"insufficient_tokens"}`.
- `GET /api/v1/messaging/widget/balance` (authed) → `{token_balance}`.
- SSE: shared stream; room events carry `room_id`; members auto-subscribed (`subscribe_many`).

## Config keys (DEFAULT_CONFIG in plugins/meinchat/__init__.py)
`guest_economy_enabled=true` · `guest_initial_tokens=20` · `guest_token_cost_per_word=1` · `widget_guest_token_ttl_hours` · `rate_widget_guest_start_*`.
Resolve economy values via `_economy_config()` (DEFAULT_CONFIG fallback), NOT raw `_meinchat_config().get(k, literal)`.

## Invariants (do not violate)
1. Widget member list + visibility are **server-trusted** (read from the stored widget via `ICmsWidgetReader`), never from the request body.
2. cms is a **soft** dependency: meinchat must enable + pass `--plugin meinchat --full` with cms absent (null reader → 404). Do NOT add cms to `PluginMetadata.dependencies`.
3. Any room member with user `role == UserRole.BOT` ⇒ room protocol `plain` (bots can't decrypt E2E). Public widgets accept BOT members only.
4. `UserRole.GUEST`: public scope only; cannot password-login (auth guard). Provisioned by meinchat, not core.
5. Billing: **1 token = 1 word** (question words + answer words). Gate = balance `> 0` at send (else 402). Charge via the post-send hook, not in `send_room_text` or the bot path.
6. `bot_base` neutrality: room parent is carried as the meinchat provider's opaque `ChatRef.chat_id = "room:<uuid>"`; do not add room/conversation discriminators to `bot_base` types.
7. The widget JSON (`docs/import/cms/widgets/meinchat-bot-widget.json`) is the source of truth for the seeded widget; the seeder imports it via the `cms_widgets` exchanger. Don't re-hardcode the widget dict.

## Seeding / CLI
```bash
flask --app "vbwd.app:create_app" data-exchange import cms_widgets \
  /app/plugins/meinchat/docs/import/cms/widgets/meinchat-bot-widget.json   # upsert by slug
# also: data-exchange list | export cms_widgets [--all|--ids] | import ... --dry-run|--mode replace_all
```
Populate order: `bot_meinchat.populate()` (provisions `assistant`) BEFORE `meinchat` populate (seeds the widget referencing `assistant`).

## Known gotchas (verified, will bite you)
- `_meinchat_config()` returns ONLY persisted overrides (`{}` fresh). Economy reads MUST fall back to DEFAULT_CONFIG (`_economy_config()`), else a fresh install grants 0 tokens → widget unusable (every send 402). Tests that patch `config_store.get_config` hide this — add a real-path test.
- The `assistant` is provisioned lazily on first 1:1 inbound; the widget needs it to pre-exist → `bot_meinchat.populate` provisions it eagerly. `on_enable` stays DB-free at boot.
- Public CMS "pages" are served as **`CmsPost type=page`** (`GET /api/v1/cms/posts/<slug>?type=page`), NOT `CmsPage`. A layout area that hosts a vue-component widget uses area type **`vue`** (the renderer dispatches on the widget's `widget_type`).
- fe-user runs Vite dev (HMR) — plugin source is live, no dist rebuild needed for meinchat plugin changes.
- fe-admin deep-link to an editor route can bounce to `/admin/login` before the auth store rehydrates; navigate client-side after login.
- `flask` in the container has no `FLASK_APP`; use `flask --app "vbwd.app:create_app" …`.

## Gate
`bin/pre-commit-check.sh --plugin meinchat --full` (and `--plugin bot_meinchat --full` if touched). NEVER commit unless explicitly told. Whole-core `--full` currently has pre-existing unrelated reds (concurrent currency/`vbwd_payment_method`/migration-graph) — not from this feature.

## Extend
- LLM bot: add a `BotCommandProvider` (namespace + `get_bot_commands` + `handle_action`) calling the LLM; make it the free-text default owner.
- Multi-bot rooms: per-bot `MeinchatInboundHook` instances keyed by `(room, bot_user_id)`; `ChatRef` room encoding already isolates sessions.
- `/tokens` purchase page: widget reads `config.buy_tokens_href` (default `/tokens`).

## Source-of-truth docs
Specs `docs/dev_log/20260613/sprints/s86-bot-widget.md` (+ s86-1/2/3) · Report `…/reports/11-s86-rooms-and-bot-widget.md` · Merchant `../merchant/bot-widget.md` · Developer `../developer/bot-widget.md`.
