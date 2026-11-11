# Changelog

Notable changes to Kiku Bot (ccbot until 2026-11). Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/) (0.x: minor = features, patch = fixes).

## [Unreleased]

### Added

- **Agent status in one tap.** With inline mode on (@BotFather `/setinline`),
  `@yourbot` in any chat lists your agents as cards: working or idle, model,
  context fill, mode, branch, session stats, and with
  `KIKUBOT_USAGE_LIMITS=true` Claude Code's usage limits with their reset
  times. `status <chat_id>:<thread_id>` also returns JSON for apps that draw
  their own screen — [docs/agent-status-format.md](docs/agent-status-format.md).
  A docker agent signed in to the host's account shows them too, once
  `DOCKER_AGENT_<NAME>_HOST_ACCOUNT=true` says so.
- **The agent's terminal as text** for apps that draw it themselves: the
  inline query `screen <chat_id>:<thread_id>` returns what the /agent photo
  shows (colours, cursor, size), and `back=<N>` adds history above it.
  Format in [docs/agent-status-format.md](docs/agent-status-format.md#screen).
  The inline query `keys` presses keys there (or `Clear`s the input box) and
  answers with the screen after them, in one round trip; keys go only to a
  pane whose agent holds its terminal
  ([format](docs/agent-status-format.md#keys)). A resent query reports what
  its keys did instead of typing them again, keys that arrive more than 8 s
  late are refused, and an unchanged screen is answered with its meta alone.
- **Session actions** in the status JSON (`commands`): new agent, worktree,
  compact, clear, rewind, resume, background, edit screenshots, restart, end,
  each a command the topic accepts, grouped and marked for confirmation, for
  a client's menu ([format](docs/agent-status-format.md#session-actions)).
  New commands behind them: `/sibling` and `/worktree` (the panel's ➕ and
  🌳), `/bgtask` (Ctrl+B on the running tool, only while one runs) and
  `/delete` (the panel's 🗑: only for a worktree agent or a sibling, with the
  same confirm, never for a topic's own agent).
- 🌳 (an agent on its own branch) is offered in any agent's git-project
  topic, Codex and Grok included: the bot makes the worktree itself and the
  new topic picks its own agent.
- **`/mode`** shows the agent's permission mode; `/mode plan` (or `manual`,
  `accept_edits`, `auto`) switches it.
- **`/role` — a topic's standing instructions.** `/role <text>` gives the
  topic a role that outlives every session in it: each Claude session the
  topic starts, resumes, clears or compacts gets it from the SessionStart
  hook, and a running agent is told at once. `/role` shows it, `/role off`
  removes it. A container agent gets it once its own SessionStart hook reads
  the file the bot keeps beside its session map
  ([docker-agents.md](docs/docker-agents.md)). Apps get it as the session action `role` (it takes text: `input`) and its
  current text from the inline query `role <chat_id>:<thread_id>`
  ([format](docs/agent-status-format.md#role)).

### Changed

- **Renamed: ccbot is now Kiku Bot** — package and command `kikubot`, data dir
  `~/.kikubot`, variables `KIKUBOT_*`, tmux session `kikubot`, plugins
  `kikubot.<name>`, repository `MrCryptoHat/kikubot`. The old names are not
  read any more: an existing install upgrades through commit `aeed5005d18f`,
  which moves it over by itself on its first start while agents keep running.
  See [docs/upgrading-from-ccbot.md](docs/upgrading-from-ccbot.md).
- **No bottom menu any more.** The 🖥️ Server / 👾 Agent keyboard opened by
  itself, after every sent message or on entering the topic, so it is gone:
  each topic that may hold it gets it removed with the bot's next message
  there. Type `/` instead: `/agent` (the control panel, was `/commands`) and
  `/server` (the summary, was `/status`) lead the command list. `/commands`,
  `/status` and `/menu` still work, unlisted.

### Fixed

- A topic now remembers its own conversations. Bringing an agent back (🔄,
  a dead window, `KIKUBOT_AUTO_RESUME_AGENTS` after a reboot) used to take
  the folder's newest session, which in a folder shared by several topics
  was often another topic's; it now takes the topic's own last one. The
  session picker lists the topic's conversations first, marked ⭐, and the
  ones other topics ran last.
- A renamed topic keeps its new name. Its agent's window and session took the
  name only while that window lived: the next one (🔄 on a dead window, a
  reboot, a new session) came back with the folder name, or a ➕/🌳 topic's
  old name, and 🔄 even renamed a live ➕/🌳 window back.
- A busy agent's status JSON over Telegram's size cap no longer loses its
  session actions: the edited-file list and other long lists go first.
- A Codex prompt with its newer key-hint footer («enter continue · esc skip»,
  e.g. its update prompt) is shown as a dialog again. A message typed into
  the update prompt picked the preselected «Update now», which quit Codex.
- A just-launched agent is no longer reported as stopped while its shell is
  still starting up.
- The first reply after a quiet minute or more no longer waits for a new
  connection to Telegram: the bot keeps one warm.

- A failed tmux read no longer reads as "every window is gone": the bot used
  to unbind a working agent's topic on one such read and kill its window 90 s
  later. tmux is driven through its CLI with short formats accepted only
  whole (libtmux dropped), and a live window that lost its topic goes back to
  it instead of being killed.
- 🔄 and `/restart` no longer stop an agent's background work without asking.
  Claude Code asks before quitting while shells or scheduled tasks run, with
  «Exit and stop tasks» preselected, and the restart used to press Enter
  there. It now cancels, lists that work in the topic and restarts only after
  «Restart and stop it». A restart that can't quit the agent no longer types
  the relaunch into it.
- A message to an agent that has quit (its window back at the shell) is no
  longer typed into the shell, where it would run as a command; the bot says
  so and points to `/restart`.
- A restart waits its turn safely: it refuses while the agent waits in a
  dialog (Enter would pick the highlighted answer), clears a leftover draft
  before `/exit`, refuses a second tap, and holds the agent's input until
  the new CLI is up, so a message can't land in the shell in between.
- An agent that dies right after an automatic revive is revived once per
  10 minutes instead of in a loop, and a session with no transcript to
  resume restarts clean instead of failing on `--resume`.
- Container agents: 🔄 and `/restart` check for a running turn, a dialog and
  background shells first; an old ▶️/🆕 offer no longer restarts an agent
  that is already back; `/kill` keeps the topic's binding like ⏹; a restart
  in progress is no longer mistaken for a dead sibling and revived over; a
  message that wakes an agent no longer answers its resume dialog.
- Text that ended in `;` lost it on the way to the agent: tmux read the
  trailing semicolon as its own command separator. Messages, chunks and keys
  are now typed exactly.
- The panel's «Wipe input» and the draft cleanup before a restart erased only
  up to the cursor, and a fresh session's input box (near the top of an empty
  screen) wasn't found at all; a leftover draft could ride along with the
  restart's `/exit` as a prompt. The box is now emptied whole and checked.
- A restart no longer types `/exit` into a pane whose agent already quit, and
  refuses rather than types when it can't read the pane.
- A 🔄 tap while the agent's screen couldn't be read raised an error and left
  the tap unanswered; it now says so.
- A container agent stopped with ⏹ and later started by its container (not by
  the bot) stayed marked as stopped, so its next real death was taken for a
  deliberate stop: no revive, no notice. Seeing it run now clears the mark.
- A long message (3800+ characters) that arrived as a single piece was lost:
  the bot held it for a second in case Telegram had split it, then cancelled
  its own delivery without a trace. It now reaches the agent.

## [0.1.0] — 2026-07-16

First tagged release — the feature set the public repo launched with.

### Core

- **Topic-based sessions**: one Telegram Forum topic = one binding = one agent
  session; tmux windows on the host or (opt-in) Claude Code inside docker
  containers.
- **Live delivery** of agent replies via transcript polling (hook-based
  session tracking, byte-offset incremental reads); MarkdownV2 with automatic
  plain-text fallback; long tables / box-art / code delivered out-of-band as
  images or file attachments.
- **Interactive prompts as screenshots** (AskUserQuestion, plan mode,
  permission prompts, sign-in menus, generic numbered menus) with an inline
  `↑ ↓ ⏎ Esc` keyboard; AskUserQuestion additionally surfaces its prose as
  text.
- **Agent panel** (`/screenshot`): live pane view with three tabs of key
  presses, everyday actions and session lifecycle (resume / new / restart /
  end), gated to what the bound agent's runtime actually supports.
- **Agent runtimes as a first-class axis**: Claude Code (default) and OpenAI
  Codex ship built-in; a runtime-tabbed session picker (tabs appear only for
  installed CLIs), per-topic runtime memory across window deaths, runtime-aware
  busy detection, `/diff` edit screenshots, image input (Codex native
  composer), history, `/esc`, and a CLI self-update canary. Adding a runtime
  is one `AgentRuntime` subclass — see `docs/adding-a-runtime.md`.
- **Worktree agents**: fork a repo into a `git worktree` + branch from a
  button, run a parallel agent (either runtime) in its own topic, with a
  two-tier teardown guard that never silently destroys unmerged work.
- **Voice**: inbound transcription (Deepgram → OpenAI fallback) and optional
  TTS replies (Gemini / ElevenLabs / OpenAI) with a `[chat]…[/chat]`
  text/voice interleave protocol.
- **Quality-of-life**: task pinning, 👍-to-confirm, 👀 read-acks, bilingual
  UI (ru/en, `/lang`), per-topic message queues with merging and a
  three-lane rate limiter tuned to Telegram's real limits.
- **Deployment**: two required env vars (`TELEGRAM_BOT_TOKEN`,
  `ALLOWED_USERS`), everything else optional with sane defaults; optional
  plugin packages for server-specific integrations; localhost task-injection
  socket (`CCBOT_INJECT_TOKEN`) with a hardened input sanitizer.

[0.1.0]: https://github.com/MrCryptoHat/ccbot/releases/tag/v0.1.0
