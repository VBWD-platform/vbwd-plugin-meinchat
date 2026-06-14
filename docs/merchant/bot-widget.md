# Meinchat Bot Widget — Merchant Guide

A drop-in **chat box** you place on any page of your site. Visitors click **Start
Conversation** and chat with your **bot** (the "assistant") or a member of your
team — right inside the page, no separate app.

This guide is for site owners / shop managers. No coding needed.

---

## What it is

- A **widget** you add to a page through the CMS, exactly like a banner, a menu,
  or a contact form.
- When a visitor opens the page, they see a small chat window with your
  **welcome message** and a **Start Conversation** button.
- You decide **who the visitor talks to** (one or more chat partners, by their
  nickname) and **who is allowed to use it** (everyone, or only logged-in
  customers).
- Chat partners can be **automated bots** (e.g. the `assistant`) or **real
  people** on your team.

---

## Adding the widget to a page

1. Go to **Admin → CMS → Widgets** and create a new widget of type
   **Meinchat Bot Widget**. (A ready-made one named **"Meinchat Bot Widget"**
   ships with the platform — you can use it as-is or copy it.)
2. Place the widget into a **page layout area** (header, main, or footer) the
   same way you place any other widget. A widget only shows on a page whose
   layout includes the area you put it in.
3. Save and open the public page — the chat window appears.

> Tip: a widget that isn't inside a layout area a page uses will not be visible.
> If you don't see it, check that the page's layout includes the area.

---

## Settings you control

| Setting | What it does |
|---|---|
| **Chat partners (nicknames)** | The nickname(s) the visitor is connected to on *Start Conversation*. Enter one or more, comma-separated (e.g. `assistant`). These can be bots or team members. |
| **Visibility** | **Public** — anyone can chat (a logged-out visitor is asked for a name). **Logged-in only** — the visitor must be signed in. |
| **Title** | The heading shown on the chat window. |
| **Welcome message** | The first message the visitor sees. |
| **Start button label** | The button text (default *Start Conversation*). |
| **Composer placeholder** | The grey hint text in the message box. |
| **Display** | **Inline** (sits in the page) or **Dock** (a floating panel in the corner). |
| **Open by default** | Whether the dock panel starts open. |

> **Public widgets must talk to a bot.** If you set *Public* but list a real
> person as the only partner, the chat won't start (we don't expose your staff
> to anonymous visitors). Use *Logged-in only* to connect visitors to a person.

---

## What the visitor experiences (the chat window)

1. **Public page, logged out:** the visitor is asked to **enter a name**. That
   name becomes their temporary nickname for the conversation.
2. **Logged-in visitor without a nickname:** they're asked to **choose a
   nickname** once (saved to their profile).
3. They click **Start Conversation** → the chat opens with your welcome message.
4. They type a message; your bot/team member replies in the same window, live.

Anonymous visitors are **guests** — a lightweight, temporary identity. They can
only use this chat; they can't sign in or see anything else on your site.

---

## The token economy (pay-per-word, optional)

You can **meter** how much an anonymous guest can chat, using tokens.

- **1 token = 1 word.** Every word the guest types **and** every word the bot
  replies with costs 1 token.
- Each new guest starts with a **token budget you set** (the *initial tokens*
  amount in settings).
- The chat works while the guest still has tokens. When the budget runs out,
  the guest sees **"Buy tokens to continue dialogue."**
- You can turn the whole token economy **off** — then guest chat is free.

**Where to set it:** Admin → Plugins → **Meinchat** → the *Guest economy* tab:

| Setting | Meaning | Default |
|---|---|---|
| **Guest economy enabled** | Turn metering on/off | On |
| **Initial tokens** | Starting budget per guest | 20 |
| **Token cost per word** | Tokens charged per word | 1 |

> Practical note: a chatty bot answer can use many words, so set **Initial
> tokens** high enough for a useful conversation (e.g. a few hundred). You decide
> the amount.

> The "Buy tokens" button currently points to a tokens page that is coming soon.
> Until then it simply signals that the guest is out of budget.

---

## Setting up your bot

The widget connects to chat partners **by nickname**. For a bot like the
`assistant`, that bot account must exist. The platform provisions the
`assistant` bot during setup. To use a **different** bot or a **team member**,
make sure that account exists and has the nickname you put in the widget
settings (you can create accounts under **Admin → Users**).

---

## In short

Place the widget → pick who answers and who may chat → write a welcome message →
(optionally) set a token budget. Visitors get a live chat with your bot or team,
right on the page.
