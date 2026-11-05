# Upgrading from ccbot

Kiku Bot was called **ccbot** until November 2026. If you run an install from
before the rename, update the checkout and restart the bot — that's all most
installs need:

```bash
git pull                     # GitHub redirects the old repository URL
./scripts/restart.sh         # stops the old `ccbot` process, starts `kikubot`
```

## What moves by itself

On its first start the new code moves the install once, before reading any
configuration, and logs every step as `Moved from ccbot: …`:

| Before | After |
|---|---|
| `~/.ccbot/` | `~/.kikubot/` — a symlink stays at `~/.ccbot` |
| `CCBOT_*` lines in `.env` (repo root and data dir) | `KIKUBOT_*` |
| `logs/ccbot.log*` | `logs/kikubot.log*` |
| tmux session `ccbot` | `kikubot` — renamed live, agents keep running |
| `session_map.json` keys `ccbot:@N` | `kikubot:@N` |
| `ccbot hook` in `~/.claude/settings.json` | `kikubot hook` |

A data dir you set explicitly (`CCBOT_DIR` / `KIKUBOT_DIR`) stays where it is,
and so does a tmux session name you set with `TMUX_SESSION_NAME`.

The bot refuses to start, saying why, if both the old and the new data dir or
tmux session already exist: merging them is your call. Move what you need
into the new one, then start again.

## What keeps working until 2027-01-31

Agents launched before the update still hold the old names, and files outside
the bot can't be rewritten from inside it, so for a while:

- the `ccbot` command still works (hooks of running agents call it);
- `~/.ccbot` points to `~/.kikubot` (running agents write there);
- `CCBOT_*` variables set outside the bot's `.env` — a systemd unit, a
  launchd plist, a shell profile — are read under their new names, with a
  WARNING at every start.

After that date these go away. Before it, check the things below.

## What to check yourself

- **Your service definition.** A systemd unit, launchd plist or script that
  runs `ccbot` → `kikubot`; `Environment=CCBOT_…` → `KIKUBOT_…`. The bundled
  plist is now `scripts/com.kikubot.bot.plist` (label `com.kikubot.bot`).
- **Plugins.** They are `kikubot.<name>` packages now and live in
  `src/kikubot/<name>/`; `KIKUBOT_PLUGINS` lists them. If you link plugins
  into the checkout, link them again — the startup log names leftovers in
  `src/ccbot/`. Then delete that dir.
- **Anything reading the bot's files**: the log is `~/.kikubot/logs/kikubot.log`
  now — the old `ccbot.log` is renamed, not kept; logger names are
  `kikubot.*` instead of `ccbot.*`; the `/inject` socket is
  `~/.kikubot/run/inject.sock` (the old path works through the symlink).
- **Your bot's name in Telegram**, if you want it to match: `/setname` in
  @BotFather.
