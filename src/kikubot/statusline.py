"""`kikubot statusline` — Claude Code status-line tap for the agent status card.

Claude Code hands its statusLine command a JSON blob on stdin: model, context
fill and the account's usage limits (``rate_limits``). Those limits exist
nowhere else, so with ``KIKUBOT_USAGE_LIMITS=true`` kikubot launches its own
Claude windows with ``--settings <agent-settings.json>`` whose statusLine is
this command. It does two things, in this order:

1. Writes a snapshot of that blob's non-private fields to
   ``<status_dir>/<window_id>.json`` (tmp + rename, 0600), for the window the
   pane belongs to — only for panes in kikubot's own tmux session.
2. Runs the user's own statusLine command (local > project > user settings,
   the precedence Claude Code itself applies) with the same stdin, passing its
   stdout, stderr and exit code through. No command of their own → prints
   nothing, which Claude Code draws as no status line at all.

Step 1 never breaks step 2: any failure there is swallowed. Must stay fast and
import-light (it runs on every status-line refresh) — no config.py, no
telegram.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# The marker that identifies our own command in a settings file — chaining to
# it would recurse.
STATUSLINE_SUBCOMMAND = "statusline"

# Fields copied into the snapshot. Paths, cwd, repo and transcript stay out.
_CONTEXT_FIELDS = ("used_percentage", "context_window_size", "total_input_tokens")
_LIMIT_FIELDS = ("used_percentage", "resets_at")
_COST_FIELDS = (
    "total_cost_usd",
    "total_duration_ms",
    "total_lines_added",
    "total_lines_removed",
)


def _snapshot(data: dict) -> dict:
    """The non-private subset of the statusLine stdin worth keeping."""
    out: dict = {"ts": int(time.time())}
    sid = data.get("session_id")
    if isinstance(sid, str):
        out["session_id"] = sid
    model = data.get("model")
    if isinstance(model, dict):
        out["model"] = {
            k: model[k] for k in ("id", "display_name") if isinstance(model.get(k), str)
        }
    ctx = data.get("context_window")
    if isinstance(ctx, dict):
        out["context_window"] = {
            k: ctx[k] for k in _CONTEXT_FIELDS if isinstance(ctx.get(k), int | float)
        }
    if isinstance(data.get("version"), str):
        out["version"] = data["version"]
    style = data.get("output_style")
    if isinstance(style, dict) and isinstance(style.get("name"), str):
        out["output_style"] = style["name"]
    effort = data.get("effort")
    if isinstance(effort, dict) and isinstance(effort.get("level"), str):
        out["effort"] = effort["level"]
    thinking = data.get("thinking")
    if isinstance(thinking, dict) and isinstance(thinking.get("enabled"), bool):
        out["thinking"] = thinking["enabled"]
    if isinstance(data.get("fast_mode"), bool):
        out["fast_mode"] = data["fast_mode"]
    cost = data.get("cost")
    if isinstance(cost, dict):
        out["cost"] = {
            k: cost[k] for k in _COST_FIELDS if isinstance(cost.get(k), int | float)
        }
    limits = data.get("rate_limits")
    if isinstance(limits, dict):
        kept = {}
        for name, block in limits.items():
            if isinstance(name, str) and isinstance(block, dict):
                fields = {
                    k: block[k]
                    for k in _LIMIT_FIELDS
                    if isinstance(block.get(k), int | float)
                }
                if fields:
                    kept[name] = fields
        if kept:
            out["rate_limits"] = kept
    return out


def _pane_window(tmux_session: str) -> str | None:
    """The ``@id`` of the window this pane is in, if it's in ``tmux_session``."""
    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        return None
    try:
        res = subprocess.run(
            [
                "tmux",
                "display-message",
                "-t",
                pane,
                "-p",
                "#{session_name}\t#{window_id}",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    session, _, window = res.stdout.strip().partition("\t")
    if session != tmux_session or not window.startswith("@"):
        return None
    return window


def write_snapshot(raw: bytes, status_dir: Path, tmux_session: str) -> None:
    """Persist the snapshot for this pane's window. Raises on any failure."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        return
    window = _pane_window(tmux_session)
    if window is None:
        return
    status_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = status_dir / f"{window}.json"
    tmp = status_dir / f".{window}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(_snapshot(data), fh)
    os.replace(tmp, target)


def _settings_statusline(path: Path) -> str | None:
    """The statusLine command a settings file sets, or None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sl = data.get("statusLine") if isinstance(data, dict) else None
    if not isinstance(sl, dict) or sl.get("type", "command") != "command":
        return None
    cmd = sl.get("command")
    return cmd if isinstance(cmd, str) and cmd.strip() else None


def _is_own_command(cmd: str) -> bool:
    parts = cmd.split()
    return (
        len(parts) >= 2
        and parts[0].endswith("kikubot")
        and parts[1] == STATUSLINE_SUBCOMMAND
    )


def user_statusline_command(raw: bytes) -> str | None:
    """The statusLine command the user would have without our --settings."""
    project: Path | None = None
    try:
        data = json.loads(raw)
        ws = data.get("workspace") if isinstance(data, dict) else None
        cwd = (ws or {}).get("project_dir") or data.get("cwd")
        if isinstance(cwd, str) and cwd:
            project = Path(cwd)
    except (ValueError, AttributeError):
        project = None
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    user_dir = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    candidates: list[Path] = []
    if project is not None:
        candidates += [
            project / ".claude" / "settings.local.json",
            project / ".claude" / "settings.json",
        ]
    candidates.append(user_dir / "settings.json")
    for path in candidates:
        cmd = _settings_statusline(path)
        if cmd and not _is_own_command(cmd):
            return cmd
    return None


def statusline_main(argv: list[str]) -> int:
    """Entry point: ``kikubot statusline <status_dir> <tmux_session>``."""
    raw = sys.stdin.buffer.read()
    if len(argv) >= 2:
        try:
            write_snapshot(raw, Path(argv[0]), argv[1])
        except Exception:  # noqa: BLE001 — never break the user's status line
            pass
    cmd = user_statusline_command(raw)
    if cmd is None:
        return 0
    try:
        return subprocess.run(cmd, shell=True, input=raw, check=False).returncode
    except OSError:
        return 1
