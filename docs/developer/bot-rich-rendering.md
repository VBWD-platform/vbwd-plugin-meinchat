# meinchat — rich bot-message rendering (`message.meta`) + portable bot styles

**Since:** S70 (2026-06-11). Companion to `plugins/bot_base/docs/developer/rich-messages.md` (the provider-neutral contract) and the iOS plan (`docs/dev_log/20260610/sprints/s71-ios-meinchat-rich-choice-rendering.md`).

In-chat the storefront/assistant bot renders as **styled cards / a command menu / a cart** instead of a plain-text numbered dump. This is enabled by a generic structured-content field on meinchat messages plus fe-user rendering, themed by a **portable** style that exports through the unified S46 Import/Export framework.

## 1. The capability — `meinchat_message.meta`

`Message` (`meinchat/models/message.py`) has a nullable **`meta` JSON** column. It rides **alongside** the plain `body`: for a bot reply, `body` keeps the human-readable + **fallback** text (so a non-rich client — e.g. today's iOS — is unaffected), and `meta` carries the structured content. e2e (`envelope`) rows are unaffected; rich `meta` is used only on **plain** bot conversations.

`MessageService.send_text(..., meta=None)` persists it; `to_dict()` returns it; `POST …/conversations/<id>/messages` accepts an optional `meta` for **plain** conversations.

### Validation (`_validate_meta`)
`meta` is validated server-side (size-capped ~8 KB). Whitelisted `kind`s + shapes:

| kind | shape |
|---|---|
| `bot_choices` | optional `text: str` + the message's `choices` (each `{label, action_data, hint?}`) |
| `bot_menu` | `commands: [{command: str, description: str}]` |
| `bot_cart` | `items: [{name, quantity:int, unit_price, line_total}]`, `total`, `currency` |
| `bot_action` | `action_data: str` (a tapped card, client → server) |

`action_data` is stored **opaque** (never parsed by meinchat). Unknown kinds are stored opaque + size-capped. Malformed → `ValueError` → HTTP 400.

## 2. Adapter translation (`bot_meinchat`)

`bot_meinchat` (the meinchat `IMessengerProvider`) maps a `bot_base` `BotReply` onto a meinchat message:
- if the reply has `choices` → `meta = {"kind": reply.meta.kind or "bot_choices", "choices":[{label, action_data, hint?}], "text"?: reply.meta.text}`
- elif `reply.meta` → it passes straight through (`bot_cart`, `bot_menu`)
- the plaintext numbered/menu `body` is built independently as the fallback.

An inbound tap (`{"kind":"bot_action","action_data":…}`) is lifted to `BotInbound.action_data` and dispatched by namespace.

## 3. fe-user rendering (`vbwd-fe-user/plugins/meinchat`)

`MessageBubble.vue` renders by `meta.kind` on **incoming** messages and **suppresses the plain `body`** for a known kind (the `body` is only the fallback):
- **`bot_choices`** — choice cards (number badge + label + right-aligned `hint`); the bubble shows `meta.text` (clean prompt) instead of the numbered body.
- **`bot_menu`** — command rows (command + description); tapping a row resends the command.
- **`bot_cart`** — a cart card: item rows (`name ×qty line_total`), **Total**, currency, and a **Proceed to checkout** button (sends `/checkout`); empty cart → an empty state. No client price math — server strings are rendered as-is.

A card/row tap sends `{ body, meta:{kind:"bot_action", action_data} }` through the normal send path. Unknown / no `meta` → the plain `body` renders unchanged.

## 4. Storefront UX note — the cart is always shown on add

The `subscription` storefront (the bot's commerce consumer) returns a **`bot_cart`** reply after **every add/toggle** (tapping a plan / add-on / token bundle), not a terse "added" confirmation — so the user always sees the running cart + the **Proceed to checkout** button the moment anything is added. `/cart` shows it on demand, `/cart-edit` lists removable items, `/cart-clear` empties it. (Implementation lives in the `subscription` plugin; meinchat just renders the `bot_cart` it receives.)

## 5. Portable bot-conversation style (`bot_meinchat`, exported via S46)

All bot-chat visuals are CSS custom properties `--vbwd-botchat-*` (card bg/border/radius/fg, accent, badge bg/fg, hint, gap). The active values come from a **`BotConversationStyle`** entity (`bot_meinchat`): a whitelisted, sanitised `tokens` map (no CSS injection), with admin CRUD + "set active" and a **public** `GET /api/v1/bot-conversation-style/active` → `{ name, tokens }` that the fe fetches on conversation mount and applies as the `--vbwd-botchat-*` vars.

`BotConversationStyle` is registered as a `BaseModelExchanger` in the **unified S46 data-exchange framework** (`settings` cluster), so it appears on **Settings → Import/Export** and round-trips JSON/CSV — **export the bot-chat look from one instance and import it into another.**

## 6. iOS

Native apps consume the same `meta` contract + the active-style endpoint (render cards/menu/cart natively, map the `tokens` to a native theme, fall back to `body` when absent). See the S71 iOS sprint.
