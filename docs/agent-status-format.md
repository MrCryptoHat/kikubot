# Agent status format (v1)

Kiku Bot answers an inline query with the status of a topic's agent: context
fill, usage limits, model, mode, what it is doing. A person sees a readable
card in any Telegram app; a client that wants to draw its own screen reads the
JSON described here. Nothing in it is specific to one client.

## Turning it on

1. In @BotFather: `/setinline`, pick the bot, placeholder text `status`.
   Until then Telegram sends the bot no inline queries and nothing changes.
2. Optional, for Claude Code's usage limits, effort and session stats: set
   `KIKUBOT_USAGE_LIMITS=true` in `.env` and restart the bot. Claude Code
   reports limits only to its status-line command, so the bot then launches
   its Claude windows with `--settings ~/.kikubot/agent-settings.json`, whose
   status line is `kikubot statusline`: it records the numbers and runs your
   own status line unchanged. Windows started before that report limits after
   their next restart; until then any window that does report stands in
   (limits belong to the account).

## The query

| Query | Results |
|---|---|
| `status <chat_id>:<thread_id>` | `status` (the card) and `status.v1.json` (JSON) for that topic's agent |
| empty, `status`, or a name | one card per agent of the asking user, filtered by name |

`chat_id` is the Bot API form (`-100…`), `thread_id` the topic's
`message_thread_id`. They only select: the bot resolves the topic through the
asking user's own bindings and requires the stored chat id to match. Only
users in `ALLOWED_USERS` get results; answers are `is_personal`,
`cache_time=0`. Asking every few seconds while a screen is open is fine: the
answer is built from the bot's caches and never touches the agent.

The JSON is the `message_text` of the `status.v1.json` result
(`InlineQueryResultArticle` / `InputTextMessageContent`). A client reads it
from the results without sending anything.

## JSON

```json
{
  "v": 1,
  "runtime": "claude",
  "ts": 1794298303,
  "state": "working",
  "model": "Opus 5.5",
  "model_running": "claude-sonnet-5-5",
  "context": {"pct": 43, "used": 86000, "window": 200000},
  "limits": [
    {"id": "5h", "label": "5 h", "pct_used": 62, "resets_at": 1794306400, "window_minutes": 300},
    {"id": "7d_fable", "label": "7 days · Fable", "pct_used": 4, "resets_at": 1794510000, "window_minutes": 10080}
  ],
  "summary": ["model", "effort", "mode", "context"],
  "sections": [ … ]
}
```

Rules:

- Every field except `v`, `runtime` and `ts` may be missing: a field is sent
  only when known. Clients ignore fields and section types they don't know.
- `v` changes only on a breaking change.
- Labels and titles are ready to show (the bot's language).
- No absolute paths, cwd, e-mail addresses or message text, ever. Changed
  files are named relative to the project (a file outside it: bare name).

### Typed core

| Field | Meaning |
|---|---|
| `runtime` | `claude`, `codex`, `grok`, `glm`, … |
| `ts` | unix seconds the answer was built |
| `state` | `working`, `idle`, `waiting` (a question or permission prompt waits for the user), `gone` (no agent running) |
| `model` | the model selected for the session |
| `model_running` | present only when the model that answered last differs from `model` (e.g. after an automatic switch) |
| `context` | `pct` of the window used, `used` tokens, `window` size |
| `limits[]` | `id` (`5h`, `7d`, `7d_<model>`, or the CLI's own name), `label`, `pct_used`, `resets_at` (unix), `window_minutes` |

A limit whose window already reset is left out rather than shown stale.

### `summary` — the first screen

Ids of the sections to show first, in order; everything else goes behind a
«more» step. Without it, show the `meter` and `choice` sections first.

### `sections` — the server-driven sheet

An ordered list; draw them in order, skip a type you don't know. Each has
`type`, `id`, `title`.

| `type` | Fields |
|---|---|
| `meter` | `pct`, `caption` («86k of 200k») |
| `meters` | `items[]`: `label`, `pct`, `caption` («38% left»), `resets_at` |
| `rows` | `items[]`: `id`, `label`, `value` |
| `choice` | `options[]`: `id`, `label`, `subtitle`, `icon`; `selected`; `command` — a template, `{id}` replaced by the picked option's id (no `{id}`: send it as is); optional `subtitle` for the whole choice |
| `actions` | `items[]`: `label`, `cmd` |

Sections sent today, in this order:

| id | type | notes |
|---|---|---|
| `model` | choice | Claude Code: `opus` / `fable` / `sonnet` / `haiku`, command `/model {id}`; the selected option's label is the full name («Opus 5.5»). The choice also becomes the default for new sessions. A runtime without aliases (Codex…) sends one option `current` and the command `/model`, which opens the CLI's own picker. |
| `effort` | choice | `low` / `medium` / `high` / `xhigh` / `max`, command `/effort {id}`. Claude Code **saves** the level for that model on the machine, so it applies to every session using the model. Sent only when the level is known (needs `KIKUBOT_USAGE_LIMITS`). |
| `context` | meter | |
| `limits` | meters | the account's limits: the freshest report of any agent on that machine |
| `mode` | choice | `manual` / `accept_edits` / `plan` / `auto` (+ `bypass` when the agent runs in it), command `/mode {id}`; left out while a dialog waits for an answer |
| `session` | rows | background shells, Remote Control (connected / on, not connected), thinking, fast mode, branch, lines changed, session time, cost at API prices, queued messages, Claude Code version, output style |
| `agents` | rows | subagents running now: `label` = their task, `value` = «running» |
| `files` | rows | files the agent edited, project-relative (`label`; `value` empty); the title carries the count |
| `commands` | actions | `/compact`, `/context`, `/model` |

Icons are names from this list; a client draws its own glyph or none:
`hand`, `code`, `plan`, `bolt`, `warning`.

### Acting

Every action is a command the bot accepts in the topic: the client sends
`cmd` (or the `choice` command with the id filled in) as an ordinary message.
`/mode <id>` is the bot's own: it presses Shift+Tab until the agent's
screen shows that mode, never while a dialog is open. The rest are passed to
the agent. A status card picked from the inline results and sent into a
topic is not passed to the agent. A client
never sends keystrokes or callback data of its own.

## Planned (same version, new sections)

- `context` breakdown (messages, tools, memory files, skills, MCP…): needs a
  `/context` read of the agent's screen, so it will come as a section with
  `"lazy": true`, fetched by `status <chat_id>:<thread_id> <section_id>` when
  the user opens it, and only while the agent is idle.
- Connected MCP servers / connectors.

Changed files and subagents are counted from when the bot started reading
the session (a bot restart starts them over).

## Extending (bot side)

A «label: value» fact is one function in `agent_status.ITEM_PROVIDERS`; it
reaches every client with no client release. Data a client should draw in its
own way gets a section type here first.
