"""The move from the old name, ccbot, to kikubot — DELETE after 2027-01-31.

Kiku Bot was called ccbot until 2026-11. This module is the whole transition,
so removing it is one commit: this file, its call sites (grep "legacy"), the
`ccbot` script in pyproject.toml, the `ccbot` match in scripts/restart.sh and
`/src/ccbot/` in .gitignore. By then every install has started once on the
new code (which migrates it) and every agent launched before has restarted.

  - migrate(): moves an existing install once, at bot startup, before config
    is read — ~/.ccbot → ~/.kikubot (a symlink stays behind for agents that
    still hold the old path), CCBOT_* names in its .env files, the log files,
    and the tmux session ccbot → kikubot with its session_map keys (renamed
    live: agents keep running, window ids don't change). A migrated install
    is left alone, so it runs on every start.
  - adopt_env(): CCBOT_* variables set outside the bot's own files (a systemd
    unit, a shell profile) still count, under the new name; the old names
    leave the environment so they can't leak into agents.
  - unmigrated_dir() / dir_from_env(): where the data lives until migrate()
    has run. Agents' hooks run the new code as soon as it is installed, and
    must keep writing where the still-running old bot reads.
  - is_hook_command(): a `ccbot hook` entry in Claude Code's settings.

Standard library only: the hook imports it, and so does startup before config.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Mapping, MutableMapping
from pathlib import Path

OLD_PREFIX = "CCBOT_"
NEW_PREFIX = "KIKUBOT_"
OLD_DIR = ".ccbot"
NEW_DIR = ".kikubot"
OLD_SESSION = "ccbot"
NEW_SESSION = "kikubot"
OLD_LOG = "ccbot.log"
NEW_LOG = "kikubot.log"
OLD_COMMAND = "ccbot"

# tmux -L socket for migrate(); "" = tmux's default. Tests point it elsewhere.
TMUX_SOCKET = ""

_ENV_LINE_RE = re.compile(r"^(\s*(?:export\s+)?)" + OLD_PREFIX, re.MULTILINE)
_SEP, _END = "␟", "␞"


class MigrationError(Exception):
    """The install can't be moved without a human: both old and new exist."""


def dir_from_env(environ: Mapping[str, str] = os.environ) -> str:
    """An explicit data dir under the old variable name, or ""."""
    return environ.get(OLD_PREFIX + "DIR", "")


def unmigrated_dir(home: Path | None = None) -> Path | None:
    """~/.ccbot while it is still the real data dir (not yet migrated)."""
    home = home or Path.home()
    old, new = home / OLD_DIR, home / NEW_DIR
    if old.is_dir() and not old.is_symlink() and not new.exists():
        return old
    return None


def adopt_env(environ: MutableMapping[str, str]) -> list[str]:
    """Rename CCBOT_* variables to KIKUBOT_* in place; return the adopted names.

    A new name already set wins. The old names are removed either way.
    """
    adopted = []
    for name in [n for n in environ if n.startswith(OLD_PREFIX)]:
        value = environ.pop(name)
        new = NEW_PREFIX + name[len(OLD_PREFIX) :]
        if new not in environ:
            environ[new] = value
            adopted.append(name)
    return adopted


def is_hook_command(cmd: str) -> bool:
    """Is this a SessionStart hook command of the old `ccbot` executable?"""
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return False
    return len(parts) == 2 and parts[1] == "hook" and Path(parts[0]).name == OLD_COMMAND


def _dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def _rewrite(path: Path, text: str) -> None:
    """Replace a file atomically, keeping its permission bits."""
    tmp = path.with_name(path.name + ".kikubot-tmp")
    tmp.write_text(text)
    os.chmod(tmp, path.stat().st_mode & 0o7777)
    os.replace(tmp, path)


def _tmux(*args: str) -> subprocess.CompletedProcess[str]:
    socket = ["-L", TMUX_SOCKET] if TMUX_SOCKET else []
    return subprocess.run(
        ["tmux", *socket, *args], capture_output=True, text=True, timeout=15
    )


def _tmux_sessions() -> dict[str, str] | None:
    """name -> $id of every tmux session; None when tmux can't be read whole."""
    try:
        res = _tmux(
            "list-sessions", "-F", f"#{{session_id}}{_SEP}#{{session_name}}{_END}"
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if res.returncode != 0:
        absent = ("no server running", "No such file or directory")
        return {} if any(m in res.stderr for m in absent) else None
    sessions = {}
    for line in res.stdout.splitlines():
        if not line.endswith(_END) or line.count(_SEP) != 1:
            return None  # cut by tmux's format time limit
        sid, name = line[: -len(_END)].split(_SEP)
        sessions[name] = sid
    return sessions


def migrate(home: Path, cwd: Path) -> list[str]:
    """Move an install from ccbot to kikubot; return what was done, for the log.

    ``home`` and ``cwd`` are explicit on purpose: a call with the real ones
    renames the live tmux session and rewrites the repo's .env.

    Raises:
        MigrationError: both the old and the new data dir (or tmux session)
            exist — merging them is a human's call.
    """
    done: list[str] = []
    local_env = _dotenv(cwd / ".env")

    # 1. The data dir — only the default one moves; an explicit dir stays put.
    explicit = (
        os.environ.get(NEW_PREFIX + "DIR")
        or os.environ.get(OLD_PREFIX + "DIR")
        or local_env.get(NEW_PREFIX + "DIR")
        or local_env.get(OLD_PREFIX + "DIR")
    )
    if explicit:
        data_dir = Path(explicit).expanduser()
    else:
        old, data_dir = home / OLD_DIR, home / NEW_DIR
        if old.is_dir() and not old.is_symlink():
            if data_dir.exists():
                raise MigrationError(
                    f"both {old} and {data_dir} exist — move what you need into "
                    f"{data_dir}, then replace {old} with a symlink to it"
                )
            os.rename(old, data_dir)
            os.symlink(data_dir, old)
            done.append(f"moved {old} to {data_dir} (symlink left at the old path)")

    # 2. Variable names in the .env files the bot reads.
    for env_file in dict.fromkeys(
        p.resolve() for p in (cwd / ".env", data_dir / ".env") if p.is_file()
    ):
        text = env_file.read_text()
        new_text, n = _ENV_LINE_RE.subn(r"\1" + NEW_PREFIX, text)
        if n:
            _rewrite(env_file, new_text)
            done.append(f"renamed {n} {OLD_PREFIX}* variable(s) in {env_file}")

    # 3. Log files, so the history stays in one place.
    logs = data_dir / "logs"
    if logs.is_dir():
        for old_log in sorted(logs.glob(OLD_LOG + "*")):
            new_log = logs / (NEW_LOG + old_log.name[len(OLD_LOG) :])
            if not new_log.exists():
                old_log.rename(new_log)
                done.append(f"renamed log {old_log.name} to {new_log.name}")

    # 4. The tmux session, only when its name was the old default.
    session_set = (
        os.environ.get("TMUX_SESSION_NAME")
        or local_env.get("TMUX_SESSION_NAME")
        or _dotenv(data_dir / ".env").get("TMUX_SESSION_NAME")
    )
    if not session_set:
        sessions = _tmux_sessions()
        for _ in range(2):  # a listing cut by tmux's format time limit
            if sessions is not None:
                break
            time.sleep(0.2)
            sessions = _tmux_sessions()
        if sessions is None:
            raise MigrationError("tmux could not be read; start again in a moment")
        if OLD_SESSION in sessions:
            if NEW_SESSION in sessions:
                raise MigrationError(
                    f"both tmux sessions '{OLD_SESSION}' and '{NEW_SESSION}' exist — "
                    f"move the agent windows into '{NEW_SESSION}' (or kill one)"
                )
            sid = sessions[OLD_SESSION]
            res = _tmux("rename-session", "-t", sid, NEW_SESSION)
            if res.returncode != 0:
                raise MigrationError(
                    f"tmux rename-session failed: {res.stderr.strip()}"
                )
            sessions[NEW_SESSION] = sessions.pop(OLD_SESSION)
            done.append(f"renamed tmux session {OLD_SESSION} to {NEW_SESSION}")
        # The old bot exported CCBOT_DIR into the session env, so new agent
        # windows would inherit it. Also when scripts/restart.sh renamed the
        # session before this ran (it does, with the old bot stopped).
        if NEW_SESSION in sessions:
            _tmux(
                "set-environment", "-t", sessions[NEW_SESSION], "-u", OLD_PREFIX + "DIR"
            )
        # 5. Its session_map keys follow the name (under the hook's own lock).
        map_file = data_dir / "session_map.json"
        if map_file.is_file():
            with open(map_file.with_suffix(".lock"), "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                entries = json.loads(map_file.read_text() or "{}")
                old_keys = [k for k in entries if k.startswith(OLD_SESSION + ":")]
                for key in old_keys:
                    new_key = NEW_SESSION + key[len(OLD_SESSION) :]
                    value = entries.pop(key)
                    entries.setdefault(new_key, value)
                if old_keys:
                    _rewrite(map_file, json.dumps(entries, indent=2) + "\n")
                    done.append(f"renamed {len(old_keys)} session_map key(s)")

    # 6. Plugin links a ccbot-plugins checkout left in the old package dir.
    old_package = Path(__file__).resolve().parent.parent / OLD_COMMAND
    if old_package.is_dir():
        links = sorted(p.name for p in old_package.iterdir() if p.name != "__pycache__")
        if links:
            done.append(
                f"plugin links still in {old_package}: {', '.join(links)} — "
                "re-run link.sh from kikubot-plugins, then delete that dir"
            )
    return done
