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
| `screen <chat_id>:<thread_id> [back=<N>]` | the agent's terminal as text, see [Screen](#screen) |
| `keys <chat_id>:<thread_id> <seq> <token> … .` | press keys in that terminal, answered with its screen, see [Keys](#keys) |
| `role <chat_id>:<thread_id>` | `role.v1.json`: the topic's role text, for the role editor, see [Role](#role) |
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
  "summary": ["model", "effort", "mode", "context", "limits"],
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
`type`, `id`, `title`. The whole JSON must fit one Telegram message, so on
a busy agent some sections are left out: `files` first, then `agents` and
`session`; `commands` is the last to go.

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
| `limits` | meters | the account's limits: the freshest report of any agent on that machine. A docker agent gets them only when its container is signed in to the host's account (`DOCKER_AGENT_<NAME>_HOST_ACCOUNT=true`, siblings included) |
| `mode` | choice | `manual` / `accept_edits` / `plan` / `auto` (+ `bypass` when the agent runs in it), command `/mode {id}`; left out while a dialog waits for an answer |
| `session` | rows | background shells, Remote Control (connected / on, not connected), thinking, fast mode, branch, lines changed, session time, cost at API prices, queued messages, Claude Code version, output style |
| `agents` | rows | subagents running now: `label` = their task, `value` = «running» |
| `files` | rows | files the agent edited, project-relative (`label`; `value` empty); the title carries the count |
| `commands` | actions | the topic's session actions, see [Session actions](#session-actions) |

Icons are names from this list; a client draws its own glyph or none:
`hand`, `code`, `plan`, `bolt`, `warning`, and for session actions `plus`,
`branch`, `compress`, `context`, `model`, `trash`, `undo`, `history`,
`background`, `diff`, `restart`, `stop`, `delete`, `role`.

### Session actions

The `commands` section lists what can be done with the topic's session, for a
client's menu. Each item is a command the topic accepts as an ordinary
message, so it works from any Telegram app too. An item the agent's CLI
can't do is left out (Codex has no `/rewind`), and the list comes only with
a bound topic.

| Field | Meaning |
|---|---|
| `id` | stable action id (below); a client may translate by it |
| `label` | ready to show, in the bot's language |
| `cmd` | the message to send |
| `group` | `new` (makes a new topic), `session`, `danger` |
| `confirm` | present when the client should ask this question first |
| `terminal` | `true` when the result is a picker or dialog in the agent's terminal: open the terminal after sending |
| `icon` | from the list above |
| `input` | present when the action takes text, see below |

| id | cmd | group | notes |
|---|---|---|---|
| `sibling` | `/sibling` | new | another agent on the same files; the bot asks for its name in the topic |
| `worktree` | `/worktree` | new | an agent on its own branch (`git worktree`); the bot asks for the task name; only in a git project |
| `compact` | `/compact` | session | confirm |
| `context` | `/context` | session | terminal (Codex: `/status`, left out where that's the bot's own command) |
| `model` | `/model` | session | terminal: the CLI's picker |
| `clear` | `/clear` | session | confirm; the old conversation stays in the resume list |
| `rewind` | `/rewind` | session | terminal: Claude Code's picker of points to go back to |
| `resume` | `/resume` | session | terminal: the CLI's list of past conversations |
| `background` | `/bgtask` | session | sends the running tool to the background (Ctrl+B); refused while idle or in a dialog. Not the CLI's own `/background`, which detaches the whole session |
| `diff` | `/diff` | session | turns the per-edit screenshots on or off for the topic |
| `role` | `/role` | session | the topic's standing instructions for its agent; takes text (`input`). Not for a container agent |
| `restart` | `/restart` | danger | confirm; the same conversation continues |
| `end` | `/kill` | danger | confirm |
| `delete` | `/delete` | danger | only for an extra agent (a worktree agent or a sibling), as on the panel. No `confirm`: the bot answers in the topic with its own, which says what would be lost (uncommitted or unmerged work); its ✅ deletes the agent and the topic |

Items without `group` (older bots) belong under `session`.

An item with `input` asks the user for text and sends `cmd`, a space and the
text, as one message (line breaks kept):

| Field | Meaning |
|---|---|
| `prefill` | an inline query (`<prefill> <chat_id>:<thread_id>`) whose answer holds the current text: start the editor from it, so changing and adding to it are the same edit |
| `clear` | the message to send when the user empties the field |
| `max` | the longest text the bot accepts, in characters |
| `placeholder` | a hint for the empty field, ready to show |

For `role`: the bot saves the text for the topic, tells a running agent at
once, and gives it to every Claude session the topic starts, resumes, clears
or compacts. It answers in the topic either way.

### Acting

Every action is a command the bot accepts in the topic: the client sends
`cmd` (or the `choice` command with the id filled in) as an ordinary message.
`/mode <id>` is the bot's own: it presses Shift+Tab until the agent's
screen shows that mode, never while a dialog is open. The rest are passed to
the agent. A status card picked from the inline results and sent into a
topic is not passed to the agent. Keystrokes go through the `keys` query
below and nothing else: a client never makes up callback data of its own.

## Screen

`screen <chat_id>:<thread_id>` returns the topic's terminal as text, so a
client can draw it itself: sharper than the /agent photo and quicker to
refresh. The topic check, `ALLOWED_USERS`, `is_personal` and `cache_time=0`
are the same as for `status`. Unlike `status`, each query captures the pane
(read-only, a few milliseconds; it never types into it), so ask for the
visible screen every second or two while the view is open, not faster.

Results, all `InlineQueryResultArticle` + `InputTextMessageContent` without
`parse_mode`:

- `screen`: the meta.
  ```json
  {"v":1,"cols":100,"rows":50,"cursor":[2,42],"ts":1794316400,
   "tms":1794316400123,"hash":"3f9c0a1b2c4d","parts":2,"panel":12345,
   "back":0,"history":1995}
  ```
- `screen.1` … `screen.<parts>`: `{"d":"<chunk>"}`, each `message_text` at
  most 4096 characters. Join the `d` values in order.

The joined text is `capture-pane -p -e`: the same capture the /agent photo
is drawn from, colours and attributes as SGR escapes (`\u001b[…m`), trailing
blanks of a row trimmed. Every line ends with `\n`, a blank last row
included: the text holds exactly `back` + `rows` lines. The JSON keeps ESC
and leading spaces intact through Telegram. Characters it might strip or alter
(controls, format characters such as ZWJ, private-use glyphs) are `\u`-escaped.

| Field | Meaning |
|---|---|
| `cols`, `rows` | the pane size; the visible screen is the last `rows` lines of the text |
| `cursor` | `[x, y]`, 0-based, `y` counted from the top of the visible screen; `null` while the program hides the cursor |
| `ts` | unix seconds of the answer |
| `tms` | unix milliseconds of the capture: order answers by it (a poll captured before your keys can arrive after their answer) |
| `hash` | 12 hex characters of what you draw (text, cursor, size); send it back as `h=` |
| `same` | `true` only on a meta-only answer: the visible screen still matches your `h=` |
| `parts` | how many `screen.N` results follow |
| `panel` | message id of the topic's newest /agent panel, or `null` when the bot hasn't sent one since it started |
| `back` | history lines that lead the text, above the visible screen |
| `history` | history lines the terminal holds in all; `back` = `history` means the top is reached |
| `error` | only when nothing was captured (agent not running): `"unavailable"`, with `parts` 0 |
| `truncated` | `true` only when even the visible screen didn't fit the parts and its bottom was cut (a huge pane full of colour); otherwise absent |

**Unchanged screens.** `screen <chat_id>:<thread_id> h=<hash>` with the
`hash` of the screen you draw: while the visible screen is the same, the
answer is the meta alone, `"same": true` and `parts` 0 (with a fresh `ts`
and `tms`). Polling with `h=` costs a few hundred bytes a second while
nothing moves instead of the whole screen. `h=` is ignored with `back`.

**History.** `back=<N>` adds up to N lines from above the visible screen
(at most 3000), followed by the visible screen itself, in one capture, so
the two meet without a gap or a repeated line. An answer holds at most 49
parts (about 190 KB); when the lines don't fit, the oldest are dropped and
`back` says how many came. Ask for history when the user scrolls, not on
every refresh. A program on the alternate screen (a full-screen TUI) has no
history there: `history` is 0.

**Panel keys.** The /agent panel's `kb:<key>:<window>` buttons still work as
taps, but they re-render the panel photo on every press; a client drawing the
screen itself uses `keys` instead.

## Keys

`keys <chat_id>:<thread_id> <seq> [t=<unix ms>] <token> … .` presses keys in the topic's
terminal and answers with its screen right after, in one round trip. It is
the only inline query that types into an agent. Same topic check,
`ALLOWED_USERS`, `is_personal` and `cache_time=0` as `screen`; an inline query
holds at most 256 characters, so split longer input over several queries.

- `seq`: an integer that grows with every `keys` query for that topic. Start
  from a value that also grows across app restarts (a millisecond
  timestamp); a `seq` more than a day ahead of the clock is refused
  (`bad-key`) and not recorded. Keep one query in flight and coalesce taps
  made meanwhile into the next.
  - A new `seq` is recorded before its keys are sent, and what they did is
    kept. **Resending the same `seq` is always safe**: the answer carries
    that `seq`'s outcome (`sent`, `error`) with `"repeat": true` and the
    current screen, and nothing is typed again. While the first is still
    being sent, the repeat waits for it (up to 6 s; past that it answers
    `"error": "in-progress"`: resend the same `seq` again). So a resend that
    says `busy` means the keys were NOT typed: send them under a new `seq`.
  - A `seq` below the last one, or one the bot can't account for (it
    restarted since), is `stale`: nothing typed, and whether it was typed
    before is unknown. The last `seq` per topic survives a bot restart, so a
    query that waited the restart out is never typed twice.
- `t=<unix ms>`, optional, right after `seq`: when the client sent the
  query. Keys that reach the bot more than 8 s later are refused
  (`expired`), nothing sent, and the `seq` is recorded: the user was already
  told they weren't sent, and typing them now would double what he presses
  again. Date it on the bot's clock, not the phone's: a phone clock 8 s
  behind would get every query refused. Every answer carries the bot's time
  (`ts`, and `tms` when a screen was captured), so keep offset = `tms` − the
  phone's time when that answer arrived, and send the phone's time + offset.
- Tokens, checked before anything is sent; one bad token sends nothing:
  - key names (tmux): `Up` `Down` `Left` `Right` `Enter` `Escape` `Tab`
    `BTab` (Shift+Tab) `BSpace` `DC` (Delete) `Space` `Home` `End` `PPage`
    `NPage` `F1`…`F12` `C-a`…`C-y` `C-Space` `M-a`…`M-z`. No `C-z`: it
    would suspend the agent back to the shell.
  - text: `=` and the text, typed exactly (a trailing `;` included), no
    Enter implied. Percent-encode only `%`, space and control characters;
    any other character, Cyrillic included, may go raw (`=привет%20мир`).
    Control characters are refused after decoding (newline, ESC…).
  - `Clear`: empty the input box, every line of it, wherever the cursor is.
    The bot uses the CLI's own editing keys and checks the box afterwards;
    it never interrupts a running turn (no Esc, no Ctrl-C). An empty box
    counts as done. A CLI whose box the bot can't read answers
    `unsupported` (Claude Code is supported).
  - `.` ends the query and is required: a query cut short or half-typed
    sends nothing.
- `Enter`, `C-m`, `C-j`, `C-c`, `C-d` and `Clear` may only be the last key
  of a query: after an Enter that quits the agent, text would land in the
  shell and run as a host command.
- The keys go only to a pane whose agent still holds its terminal, checked
  under the pane's lock and again right before the last key. After an Enter
  or Ctrl-C/Ctrl-D (from `keys`, the panel or a message) the next write to
  that pane waits up to 1 s first, the time an agent that quits takes to let
  go of the terminal.
- Keys aren't meant to end the agent (Restart and End stay on the panel), so
  those that visibly would are refused (`would-exit`). In Claude Code: an
  Enter while the input box holds `/exit`, `/quit`, `/logout`,
  `/background` (`/bg`, which detaches the session), `/restart`, the start of
  one (`/ex`, which Enter completes) or the bare word `exit`/`quit`; an Enter
  while the slash menu highlights one of them; Enter or Esc on the
  folder-trust gate while it would quit; a Ctrl-C/Ctrl-D while the agent
  shows «again to exit». For other CLIs, an Enter after their exit command
  typed in the same query. This is a screen reading, not a guarantee; the
  terminal check above is the rule that holds.

The answer is the `screen` answer of the visible screen (`back` 0) after the
keys, with `seq`, `sent` (tokens actually sent) and `changed` in the meta.
The bot waits for the screen to change, up to 0.3 s, and answers at that cap
anyway (`"changed": false`); the next `screen` poll catches a slower redraw.
The panel photo is not touched.

The bot doesn't limit a query's length; Telegram caps an inline query at 256
characters. Count them as UTF-16 units to be safe (an emoji is 2).

| `error` | Meaning | Sent |
|---|---|---|
| `bad-key` | a token not on the list, bad text, no `.`, a key after Enter/C-c/C-d/Clear, or a `seq` far ahead | nothing |
| `stale` | `seq` below the last one, or one the bot can't account for since a restart | nothing |
| `expired` | `t=` more than 8 s old | nothing |
| `in-progress` | a repeat whose first send is still going after 6 s; resend the same `seq` | nothing yet |
| `not-running` | no agent in the pane (it quit, or a restart is under way) | the keys before it, if it quit mid-query |
| `busy` | the bot is typing a message into that pane; send them under a new `seq` | nothing |
| `would-exit` | the last key would end the agent | the keys before it |
| `unsupported` | `Clear` on a CLI whose input box the bot can't read | the keys before it |
| `unavailable` | the pane couldn't be read or written | `sent` says how many it knows of; after an internal failure some may have gone in uncounted, so check the screen |

Every answer carries the current screen when it can be read; when it can't,
the meta comes alone with `parts` 0.

## Role

`role <chat_id>:<thread_id>` answers one article, `role.v1.json`, whose
`message_text` is:

```json
{"v": 1, "role": "Only log my gym notes as tasks.\nNo questions.", "max": 2000}
```

`role` is `""` when the topic has none. Same topic check as `status`; a
topic without an agent gets no results. The text is the user's own, which is
why it is not in the status JSON.

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
