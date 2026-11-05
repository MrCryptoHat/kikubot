# Upgrading from ccbot

Kiku Bot was called **ccbot** until November 2026. The current code no longer
knows the old name, so an install from before the rename upgrades through the
last version that moves it over by itself, commit `aeed5005d18f`:

```bash
git fetch                          # GitHub redirects the old repository URL
git checkout aeed5005d18f
./scripts/restart.sh               # stops the old `ccbot` process, starts `kikubot`, moves the install
grep 'Moved from ccbot' ~/.kikubot/logs/kikubot.log   # what it moved
git checkout main
./scripts/restart.sh
```

## What the move does

On its first start that version moves the install once, before reading any
configuration, and logs every step as `Moved from ccbot: …`:

| Before | After |
|---|---|
| `~/.ccbot/` | `~/.kikubot/` — a symlink stays at `~/.ccbot` |
| what names a path in `~/.ccbot/worktrees/`: git's worktree registrations, Claude Code's transcript dirs, paths in `state.json` | the same under `~/.kikubot/worktrees/` — an agent in a worktree resumes its conversation after a restart |
| `CCBOT_*` lines in `.env` (repo root and data dir) | `KIKUBOT_*` |
| `logs/ccbot.log*` | `logs/kikubot.log*` |
| tmux session `ccbot` | `kikubot` — renamed live, agents keep running |
| `session_map.json` keys `ccbot:@N` | `kikubot:@N` |
| `ccbot hook` in `~/.claude/settings.json` | `kikubot hook` |

A data dir you set explicitly (`CCBOT_DIR` / `KIKUBOT_DIR`) stays where it is
(`CCBOT_DIR=~/.ccbot` is the default, not a choice: it moves),
and so does a tmux session name you set with `TMUX_SESSION_NAME`.

The bot refuses to start, saying why, if both the old and the new data dir or
tmux session already exist: merging them is your call. Move what you need
into the new one, then start again.

## What to change yourself before going back to `main`

`main` has no `ccbot` command and doesn't read `CCBOT_*` variables or
`~/.ccbot`. Files outside the bot are yours to change:

- **Your service definition.** A systemd unit, launchd plist or script that
  runs `ccbot` → `kikubot`; `Environment=CCBOT_…` → `KIKUBOT_…`. The bundled
  plist is now `scripts/com.kikubot.bot.plist` (label `com.kikubot.bot`).
  `aeed5005d18f` logs a WARNING at start for every `CCBOT_*` variable it
  still finds outside the bot's `.env`.
- **Plugins.** They are `kikubot.<name>` packages now and live in
  `src/kikubot/<name>/`; `KIKUBOT_PLUGINS` lists them. If you link plugins
  into the checkout, link them again — the startup log names leftovers in
  `src/ccbot/`. Then delete that dir.
- **Anything reading the bot's files**: the log is `~/.kikubot/logs/kikubot.log`
  — the old `ccbot.log` is renamed, not kept; logger names are `kikubot.*`
  instead of `ccbot.*`; the `/inject` socket is `~/.kikubot/run/inject.sock`.
- **Agents started before the move** keep running, but their Claude Code
  sessions may still call the old `ccbot hook` path. Restart each one once
  (🔄 in its topic) so it picks up `kikubot hook`.
- **Your bot's name in Telegram**, if you want it to match: `/setname` in
  @BotFather.

Keep the `~/.ccbot` symlink: transcripts and notes written before the move
name files under it (`~/.ccbot/images/…`), and the symlink keeps them working.
Nothing in the bot reads it.
