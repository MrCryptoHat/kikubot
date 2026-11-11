"""Tmux session/window management through the tmux CLI.

Async-friendly operations on a single tmux session:
  - list_windows / find_window_by_name / find_window_by_id: discover agent
    windows. A read that fails raises TmuxReadError, never an empty answer.
  - agent_running_ids / is_agent_running: does a window's agent still hold its
    terminal — the dead-window check; the rule is pane_agent_running.
  - capture_pane: read terminal content (plain or with ANSI colors).
  - send_keys: forward user input or control keys to a window.
  - create_window / rename_window / kill_window: lifecycle management.

Every call is one ``tmux`` command (blocking, run in asyncio.to_thread).
Windows are addressed by their global ``@N`` id, the session by its ``$N`` id;
names are never used as targets — see _session_id for why. Listings use short
formats of our own and are accepted only whole — see TmuxReadError for why.

Key class: TmuxManager (singleton instantiated as `tmux_manager`).
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import config
from .pane_screen import SCREEN_FORMAT, PaneScreen, parse_screen_output
from .procinfo import foreground_process_groups
from .runtimes import get_runtime
from .utils import KIKUBOT_DIR_ENV, tmux_literal_args

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaneState:
    """What tmux reports about one pane — enough to judge its agent's liveness."""

    pid: int | None  # pane_pid: the pane's root process, normally its shell
    dead: bool  # pane_dead: the root exited and remain-on-exit kept the pane
    start_command: str  # pane_start_command as tmux renders it; "" = default shell


@dataclass
class TmuxWindow:
    """Information about a tmux window."""

    window_id: str
    window_name: str
    cwd: str  # Current working directory of the active pane
    panes: list[PaneState] = field(default_factory=list)


@dataclass(frozen=True)
class TmuxResult:
    """One tmux command's outcome, stdout/stderr exactly as tmux wrote them."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class TmuxReadError(Exception):
    """tmux could not be read, so whether a window exists is UNKNOWN.

    Kept apart from an empty listing on purpose. tmux gives up expanding a
    ``-F`` row that takes over 100 ms (FORMAT_TIME_LIMIT in format.c) and
    silently prints the row cut short — or as an empty line — which happens
    under load. libtmux's 136-field rows hit that limit and its parser turned
    the failure into "no session", so the window list came back empty while
    every window was alive: status polling unbound a working agent's topic on
    that one answer, and 90 s later the orphan janitor killed the window as
    unbound. Nothing destructive may follow from this error; callers skip the
    step or report a failure instead.
    """


# Listings use short formats of our own. Each record ends with _RECORD_END
# right before tmux's newline, fixed-shape fields come first and free text
# last, so a cut row (see TmuxReadError) can't pass: it either merges with
# the next row (wrong field count, or a newline in front of the id) or leaves
# the output without its final terminator.
_FIELD_SEP = "␟"  # ␟
_RECORD_END = "␞"  # ␞


def _format(*fields: str) -> str:
    return _FIELD_SEP.join(f"#{{{f}}}" for f in fields) + _RECORD_END


# One row per pane of every session; ours are picked by exact session name.
_PANE_FIELDS = (
    "window_id",
    "pane_active",
    "pane_pid",
    "pane_dead",
    "session_name",
    "window_name",
    "pane_current_path",
    "pane_start_command",
)
_PANE_FORMAT = _format(*_PANE_FIELDS)
_SESSION_FORMAT = _format("session_id", "session_name")
_WINDOW_ID_RE = re.compile(r"@\d+")
_SESSION_ID_RE = re.compile(r"\$\d+")
# No server answered. On macOS a connection refused for a full accept backlog
# reads exactly like a dead server ("no server running"), so this is trusted
# only after every attempt says it.
_SERVER_ABSENT = ("no server running", "No such file or directory")
TMUX_CALL_TIMEOUT = 15.0  # seconds; a wedged server must not hang a thread forever
LIST_READ_ATTEMPTS = 3
LIST_RETRY_DELAY = 0.2  # seconds between attempts
_READ_WARN_INTERVAL = 30.0  # one WARNING per this many seconds; the rest DEBUG
_last_read_warning = 0.0


def _server_absent(result: TmuxResult) -> bool:
    return any(marker in result.stderr for marker in _SERVER_ABSENT)


def _split_records(
    stdout: str, n_fields: int, id_re: re.Pattern[str], what: str
) -> list[list[str]]:
    """Split a listing into records; TmuxReadError unless every one is whole."""
    if not stdout:
        return []
    terminator = _RECORD_END + "\n"
    if not stdout.endswith(terminator):
        raise TmuxReadError(f"{what} output cut short: {stdout[-300:]!r}")
    records = []
    for record in stdout[: -len(terminator)].split(terminator):
        values = record.split(_FIELD_SEP)
        if len(values) != n_fields or not id_re.fullmatch(values[0]):
            raise TmuxReadError(f"malformed {what} record: {record[:300]!r}")
        records.append(values)
    return records


def _parse_pane_records(stdout: str, session_name: str) -> list[TmuxWindow]:
    """Group the session's ``list-panes -a`` records into windows."""
    windows: dict[str, TmuxWindow] = {}
    for values in _split_records(
        stdout, len(_PANE_FIELDS), _WINDOW_ID_RE, "list-panes"
    ):
        row = dict(zip(_PANE_FIELDS, values, strict=True))
        if row["session_name"] != session_name:
            continue
        window_id = row["window_id"]
        name = row["window_name"]
        # Skip the main window (placeholder window)
        if name == config.tmux_main_window_name:
            continue
        try:
            pid = int(row["pane_pid"]) if row["pane_pid"] else None
        except ValueError:
            pid = None
        pane = PaneState(
            pid=pid,
            dead=row["pane_dead"] == "1",  # "0" is a truthy string
            start_command=row["pane_start_command"],
        )
        window = windows.get(window_id)
        if window is None:
            window = windows[window_id] = TmuxWindow(
                window_id=window_id,
                window_name=name,
                cwd=row["pane_current_path"],
            )
        elif row["pane_active"] == "1":
            window.cwd = row["pane_current_path"]
        window.panes.append(pane)
    return list(windows.values())


def _log_read_failure(err: TmuxReadError) -> None:
    """WARNING for the first failure in a while, DEBUG for the rest.

    Every bound topic polls at once, so one bad moment fails a dozen reads.
    """
    global _last_read_warning
    now = time.monotonic()
    if now - _last_read_warning >= _READ_WARN_INTERVAL:
        _last_read_warning = now
        logger.warning("tmux window listing failed: %s", err)
    else:
        logger.debug("tmux window listing failed: %s", err)


# tmux renders a pane's start command in its own quoting (args_escape): wrapped
# in "…" or '…' when it holds shell-special characters, with backslash escapes
# inside — \\ \" \$ \` and \~ for the character itself, C-style (\t, \n) or
# octal (\033) for control bytes. Not shell quoting: shlex keeps "\$" as-is.
_TMUX_ESCAPE_RE = re.compile(r"\\([0-7]{3}|.)", re.DOTALL)
_TMUX_CSTYLE = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _tmux_unescape(rendered: str) -> str:
    """Undo tmux's quoting of a single-argument start command."""
    body = rendered
    if len(body) >= 2 and body[0] == body[-1] and body[0] in "\"'":
        body = body[1:-1]

    def _char(match: re.Match[str]) -> str:
        code = match.group(1)
        if len(code) == 3:
            return chr(int(code, 8))
        return _TMUX_CSTYLE.get(code, code)

    return _TMUX_ESCAPE_RE.sub(_char, body)


def _starts_default_shell(start_command: str, default_command: str) -> bool:
    """Did tmux start this pane with its default shell rather than a program?

    Empty means tmux ran ``default-shell``. A set ``default-command`` (``set -g
    default-command "$SHELL"`` is a common dotfile line) is copied into EVERY
    command-less pane's start command in tmux's quoting (``zsh -l`` →
    ``"zsh -l"``) — compare it with that quoting undone, or every pane on such
    a host reads as "started with a program" and is never reaped. A rendering
    this doesn't decode just fails to match, i.e. reads as running.
    """
    if not start_command:
        return True
    if not default_command:
        return False
    return _tmux_unescape(start_command) == default_command


def pane_agent_running(
    pane: PaneState, foreground: Mapping[int, int], default_command: str
) -> bool:
    """Does something other than the pane's own shell hold its terminal?

    kikubot types the launch command into a shell pane, so "the agent exited" is
    exactly "the shell has the terminal back": the tty's foreground process
    group is the shell's own again (``foreground[pid] == pid``). That is a
    kernel fact, the same for every CLI, version and OS. The process NAME is
    not: codex runs as ``codex``, a native-install Claude Code as its version
    (``2.1.267`` on macOS), and after a CLI self-update the installed binary
    resolves to the NEW version while every window launched earlier still runs
    the old one. Each name-based check reaped live windows until the next
    variant — codex at launch, native claude at launch, then every window
    minutes after a background update.

    Every unknown answers "running": a false "dead" kills a live session, a
    false "running" only delays cleanup. A Ctrl-Z'd agent does hand the
    terminal back and reads as exited — deliberately, since text sent to that
    pane would now run as shell commands.
    """
    if pane.dead:
        # Before anything else: a remain-on-exit pane keeps its exited root's
        # stale pid, which the unknown-means-running rule would keep forever.
        return False
    if not _starts_default_shell(pane.start_command, default_command):
        # Started with a program (e.g. a window adopted from outside kikubot):
        # the root IS the program and holds the terminal itself, so the shell
        # rule would read it as idle. It lives exactly as long as the pane.
        return True
    if pane.pid is None:
        return True
    tpgid = foreground.get(pane.pid)
    if tpgid is None or tpgid <= 0:
        return True
    return tpgid != pane.pid


# A tmux call slower than this gets a log line (see TmuxManager._tmux).
SLOW_TMUX_SEC = 0.5


class TmuxManager:
    """Manages tmux windows for Claude Code sessions."""

    def __init__(self, session_name: str | None = None, socket_name: str = ""):
        """Initialize tmux manager.

        Args:
            session_name: Name of the tmux session to use (default from config)
            socket_name: tmux ``-L`` socket; "" = tmux's own default (tests
                point this at a throwaway server)
        """
        self.session_name = session_name or config.tmux_session_name
        self.socket_name = socket_name

    # --- tmux primitive -----------------------------------------------------

    def _tmux_sync(self, *args: str) -> TmuxResult:
        """Run one tmux command against our server. Never raises."""
        # -u: run outside tmux without a UTF-8 locale (a service unit, cron),
        # tmux prints every non-ASCII character as "_" — the ␟/␞ our listings
        # split on too, so every listing would read as cut short, and a window
        # named «app · дизайн» would never match its own name.
        argv = ["tmux", "-u"]
        if self.socket_name:
            argv += ["-L", self.socket_name]
        argv += args
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=TMUX_CALL_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            return TmuxResult(-1, "", f"tmux did not run: {e}")
        return TmuxResult(
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )

    async def _tmux(self, *args: str) -> TmuxResult:
        """``_tmux_sync`` on a worker thread, logging a call that ran slow.

        A slow line says which: the wait for a free worker thread, or tmux
        itself. Either stalls every pane read behind it (the 1 s poll, the
        inline ``screen`` a client polls every second).
        """
        submitted = time.monotonic()

        def run() -> tuple[TmuxResult, float, float]:
            started = time.monotonic()
            res = self._tmux_sync(*args)
            return res, started - submitted, time.monotonic() - started

        res, waited, ran = await asyncio.to_thread(run)
        if waited + ran > SLOW_TMUX_SEC:
            logger.warning(
                "tmux %s slow: %.0f ms waiting for a thread, %.0f ms in tmux",
                args[0] if args else "",
                waited * 1000,
                ran * 1000,
            )
        return res

    # --- session ------------------------------------------------------------

    def _session_id(self) -> str | None:
        """The session's ``$N`` id; None when tmux answered without it.

        Session-level commands take this id, never the name: tmux matches a
        ``-t <name>`` by prefix (``kikubot`` hits ``kikubot-x`` while ``kikubot`` is
        gone), show-options/set-environment refuse ``=<name>``, and
        ``list-panes -s -t =<missing>`` answers with ANOTHER session's panes.

        Raises:
            TmuxReadError: tmux couldn't be read.
        """
        res = self._tmux_sync("list-sessions", "-F", _SESSION_FORMAT)
        if not res.ok:
            if _server_absent(res):
                return None
            raise TmuxReadError(f"list-sessions rc={res.returncode}: {res.stderr!r}")
        for sid, name in _split_records(res.stdout, 2, _SESSION_ID_RE, "session"):
            if name == self.session_name:
                return sid
        return None

    def ensure_session(self) -> str:
        """Create the session if it's missing; return its ``$N`` id.

        Also re-scrubs secrets from its environment and re-exports KIKUBOT_DIR,
        on every call — new windows inherit whatever the session env holds.

        Raises:
            TmuxReadError: tmux couldn't be read, or creating the session failed.
        """
        sid = self._session_id()
        if sid is None:
            res = self._tmux_sync(
                "new-session",
                "-d",
                "-s",
                self.session_name,
                "-n",
                config.tmux_main_window_name,
                "-c",
                str(Path.home()),
            )
            sid = self._session_id()  # also covers losing a creation race
            if sid is None:
                raise TmuxReadError(f"new-session failed: {res.stderr.strip()!r}")
        self._scrub_session_env(sid)
        self._export_kikubot_dir(sid)
        return sid

    def _scrub_session_env(self, sid: str) -> None:
        """Remove sensitive env vars from the tmux session environment.

        Prevents new windows (and their child processes like Claude Code)
        from inheriting secrets such as TELEGRAM_BOT_TOKEN. Unsetting a var
        that isn't set is a no-op for tmux.
        """
        for var in config.sensitive_env_vars:
            self._tmux_sync("set-environment", "-t", sid, "-u", var)

    def _export_kikubot_dir(self, sid: str) -> None:
        """Publish the bot's resolved KIKUBOT_DIR to the tmux session env.

        The SessionStart hook runs inside agent panes and inherits the tmux
        *server* environment, not the bot's — so a KIKUBOT_DIR that reached the
        bot only via `.env` (which the hook deliberately never reads) would
        make the hook write session_map.json to ~/.kikubot while the bot reads
        it from $KIKUBOT_DIR: replies silently stop arriving. Exporting the
        resolved path into the session env makes every window created after
        this point see the same directory the bot uses.
        """
        res = self._tmux_sync(
            "set-environment", "-t", sid, KIKUBOT_DIR_ENV, str(config.config_dir)
        )
        if not res.ok:
            logger.warning(
                "Failed to export KIKUBOT_DIR into tmux session env: %s",
                res.stderr.strip(),
            )

    # --- windows ------------------------------------------------------------

    async def _list_windows_once(self) -> list[TmuxWindow] | None:
        """One ``list-panes -a`` read. None = no server answered.

        Raises:
            TmuxReadError: tmux answered with anything but a whole listing.
        """
        res = await self._tmux("list-panes", "-a", "-F", _PANE_FORMAT)
        if res.ok:
            return _parse_pane_records(res.stdout, self.session_name)
        if _server_absent(res):
            return None
        raise TmuxReadError(f"list-panes rc={res.returncode}: {res.stderr[:300]!r}")

    async def list_windows(self) -> list[TmuxWindow]:
        """List all windows in the session with their working directories.

        One ``list-panes -a`` for the whole server — every row already names
        its session and window. (``window.active_pane`` cost a tmux call per
        window, on a poll loop that lists twice a second.)

        An empty list means tmux said so: a live server without the session,
        or no server on every attempt. A read that fails on every attempt
        raises TmuxReadError instead (see there).

        Returns:
            List of TmuxWindow with window info, active-pane cwd and pane states
        """
        for attempt in range(1, LIST_READ_ATTEMPTS + 1):
            last = attempt == LIST_READ_ATTEMPTS
            try:
                windows = await self._list_windows_once()
            except TmuxReadError as e:
                if last:
                    _log_read_failure(e)
                    raise
                logger.debug("tmux listing attempt %d failed: %s", attempt, e)
            else:
                if windows is not None:
                    return windows
                if last:
                    return []
            await asyncio.sleep(LIST_RETRY_DELAY)
        raise AssertionError("unreachable")  # the last attempt returns or raises

    async def find_window_by_name(self, window_name: str) -> TmuxWindow | None:
        """Find a window by its name.

        Args:
            window_name: The window name to match

        Returns:
            TmuxWindow if found, None if tmux answered without it.

        Raises:
            TmuxReadError: tmux couldn't be read, presence unknown.
        """
        windows = await self.list_windows()
        for window in windows:
            if window.window_name == window_name:
                return window
        logger.debug("Window not found by name: %s", window_name)
        return None

    async def find_window_by_id(self, window_id: str) -> TmuxWindow | None:
        """Find a window by its tmux window ID (e.g. '@0', '@12').

        Args:
            window_id: The tmux window ID to match

        Returns:
            TmuxWindow if found, None if tmux answered without it.

        Raises:
            TmuxReadError: tmux couldn't be read, presence unknown. Anything
                that unbinds or kills on a missing window must let it stop it.
        """
        windows = await self.list_windows()
        for window in windows:
            if window.window_id == window_id:
                return window
        logger.debug("Window not found by id: %s", window_id)
        return None

    async def agent_running_ids(self, windows: Iterable[TmuxWindow]) -> set[str]:
        """IDs of the windows whose agent still holds its terminal.

        A window counts while ANY of its panes does: a user who splits an agent
        window and focuses the shell half must not get the agent reaped. A
        window with no pane data counts too — nothing to judge by. The rule
        itself is pane_agent_running.
        """
        windows = list(windows)
        panes = [p for w in windows for p in w.panes]
        # Only a pane started with a command needs default-command to be
        # judged — skip the tmux call when no pane has one.
        needs_default = next(
            (w.window_id for w in windows if any(p.start_command for p in w.panes)),
            None,
        )
        default_command = (
            await self._default_command(needs_default) if needs_default else ""
        )
        # Dead panes are never looked up: their pid is stale, maybe reused.
        foreground = await asyncio.to_thread(
            foreground_process_groups,
            [p.pid for p in panes if p.pid is not None and not p.dead],
        )
        return {
            w.window_id
            for w in windows
            if not w.panes
            or any(pane_agent_running(p, foreground, default_command) for p in w.panes)
        }

    async def is_agent_running(self, window: TmuxWindow) -> bool:
        """Whether this window's agent still holds its terminal."""
        return window.window_id in await self.agent_running_ids([window])

    async def _default_command(self, window_id: str) -> str:
        """The effective ``default-command`` of ``window_id``'s session.

        "" when unset or unknown. Unknown degrades safely: a start command that
        then fails to match is judged "started with a program", i.e. running.
        """
        res = await self._tmux(
            "show-options", "-t", window_id, "-Av", "default-command"
        )
        if not res.ok:
            return ""
        return res.stdout.split("\n", 1)[0]

    async def ensure_session_pane_size(self, cols: int, rows: int) -> None:
        """Pin the session's pane size for screenshots.

        tmux sizes panes to the smallest attached client; with no client
        attached the session falls back to ``default-size`` (80x24 by
        default). Without this override capture-pane returns 80-column
        text and Claude Code's footer wraps off-screen in /screenshot.

        Sets ``default-size`` so newly created windows inherit, then
        resizes every existing window. If a real client (SSH, mosh) is
        attached at a smaller size, the smaller size wins for the
        duration of the attach — this is a best-effort floor.
        """
        try:
            sid = await asyncio.to_thread(self._session_id)
            windows = await self.list_windows()
        except TmuxReadError as e:
            logger.warning("Pane size not pinned, tmux unreadable: %s", e)
            return
        if sid is None:
            return
        # default-size for new windows.
        await self._tmux("set-option", "-t", sid, "default-size", f"{cols}x{rows}")
        # Resize live windows.
        for window in windows:
            res = await self._tmux(
                "resize-window",
                "-t",
                window.window_id,
                "-x",
                str(cols),
                "-y",
                str(rows),
            )
            if not res.ok:
                logger.debug(
                    "resize-window failed for %s: %s",
                    window.window_id,
                    res.stderr.strip(),
                )

    async def capture_pane(
        self,
        window_id: str,
        with_ansi: bool = False,
        scrollback_lines: int = 0,
    ) -> str | None:
        """Capture the visible text content of a window's active pane.

        Args:
            window_id: The window ID to capture
            with_ansi: If True, capture with ANSI color codes
            scrollback_lines: If >0, include this many rows of scrollback
                above the visible area (passes `-S -<N>` to tmux). Use when
                the caller needs to see content that may have scrolled off
                — /context's output, for example, regularly exceeds the
                50-row viewport.

        Returns:
            The captured text, or None on failure. The plain capture has its
            trailing blank rows dropped; with ANSI or scrollback it is tmux's
            output as is.
        """
        args = ["capture-pane", "-p"]
        if with_ansi:
            args.append("-e")
        args += ["-t", window_id]
        if scrollback_lines > 0:
            args += ["-S", f"-{scrollback_lines}"]
        res = await self._tmux(*args)
        if not res.ok:
            logger.error("Failed to capture pane %s: %s", window_id, res.stderr.strip())
            return None
        if with_ansi or scrollback_lines > 0:
            return res.stdout
        return res.stdout.rstrip("\n")

    async def capture_screen(self, window_id: str, back: int = 0) -> PaneScreen | None:
        """The pane with colours, cursor and size, for the inline ``screen``.

        One tmux call: the capture and the format line come from the same
        moment, so the cursor matches the text. ``back`` adds that many
        history lines above the visible screen.
        """
        args = ["capture-pane", "-p", "-e", "-t", window_id]
        if back > 0:
            args += ["-S", f"-{back}"]
        args += [";", "display-message", "-p", "-t", window_id, SCREEN_FORMAT]
        taken = time.time()
        res = await self._tmux(*args)
        if not res.ok:
            logger.debug("screen capture of %s failed: %s", window_id, res.stderr)
            return None
        screen = parse_screen_output(res.stdout)
        return replace(screen, captured_at=taken) if screen else None

    async def send_key_batch(
        self, window_id: str, items: Iterable[tuple[str, bool]]
    ) -> bool:
        """Type ``(key, literal)`` items in order with ONE tmux call.

        The send-keys commands are joined with ``;``. Literal text goes
        through ``tmux_literal_args``, so a ``;`` it ends with is typed
        instead of splitting the commands.
        """
        args: list[str] = []
        for key, literal in items:
            if args:
                args.append(";")
            args += ["send-keys", "-t", window_id]
            args += ["-l", "--", *tmux_literal_args(key)] if literal else [key]
        if not args:
            return True
        res = await self._tmux(*args)
        if not res.ok:
            logger.error(
                "send-keys batch failed for %s: %s", window_id, res.stderr.strip()
            )
        return res.ok

    async def _send_keys_once(self, window_id: str, *keys: str) -> bool:
        res = await self._tmux("send-keys", "-t", window_id, *keys)
        if not res.ok:
            logger.error(
                "send-keys failed for window %s: %s", window_id, res.stderr.strip()
            )
        return res.ok

    async def send_keys(
        self, window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        """Send keys to a specific window.

        Literal text goes as ``send-keys -l -- <text>``: without ``--`` a text
        starting with "-" is eaten as tmux flags and the send fails.

        Args:
            window_id: The window ID to send to
            text: Text to send
            enter: Whether to press enter after the text
            literal: If True, send text literally. If False, interpret special keys
                     like "Up", "Down", "Left", "Right", "Escape", "Enter".

        Returns:
            True if successful, False otherwise
        """
        if not literal:
            if text and not await self._send_keys_once(window_id, text):
                return False
            return not enter or await self._send_keys_once(window_id, "Enter")
        if not enter:
            return await self._send_keys_once(
                window_id, "-l", "--", *tmux_literal_args(text)
            )

        # Text, a pause, then Enter on its own. Claude Code's TUI sometimes
        # takes an Enter arriving in the same input batch as the text for a
        # newline rather than submit; the gap lets it process the text first.
        if text.startswith("!"):
            # Claude Code's ! command mode: send "!" first so the TUI
            # switches to bash mode, wait 1s, then send the rest.
            if not await self._send_keys_once(window_id, "-l", "--", "!"):
                return False
            rest = text[1:]
            if rest:
                await asyncio.sleep(1.0)
                if not await self._send_keys_once(
                    window_id, "-l", "--", *tmux_literal_args(rest)
                ):
                    return False
        else:
            # Split long text into chunks to avoid tmux buffer limits
            chunk_size = 200
            for i in range(0, len(text), chunk_size):
                chunk = text[i : i + chunk_size]
                if not await self._send_keys_once(
                    window_id, "-l", "--", *tmux_literal_args(chunk)
                ):
                    return False
                if i + chunk_size < len(text):
                    await asyncio.sleep(0.1)
        # Longer delay for multi-line/long text to let TUI process paste
        delay = 1.5 if len(text) > 200 else 0.5
        await asyncio.sleep(delay)
        return await self._send_keys_once(window_id, "Enter")

    async def unique_window_name(self, name: str, *, own_id: str = "") -> str:
        """``name``, or ``name-2``, ``name-3``… if another window holds it.

        ``own_id``: a window renaming itself doesn't collide with its own name.
        Raises TmuxReadError: tmux couldn't be read.
        """
        candidate, counter = name, 2
        while True:
            found = await self.find_window_by_name(candidate)
            if found is None or found.window_id == own_id:
                return candidate
            candidate = f"{name}-{counter}"
            counter += 1

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        """Rename a tmux window by its ID."""
        res = await self._tmux("rename-window", "-t", window_id, new_name)
        if not res.ok:
            logger.error(
                "Failed to rename window %s: %s", window_id, res.stderr.strip()
            )
            return False
        logger.info("Renamed window %s to '%s'", window_id, new_name)
        return True

    async def kill_window(self, window_id: str) -> bool:
        """Kill a tmux window by its ID. False if it failed or was already gone."""
        res = await self._tmux("kill-window", "-t", window_id)
        if not res.ok:
            if "can't find window" not in res.stderr:
                logger.error(
                    "Failed to kill window %s: %s", window_id, res.stderr.strip()
                )
            return False
        logger.info("Killed window %s", window_id)
        return True

    async def create_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
        runtime: str = "claude",
    ) -> tuple[bool, str, str, str]:
        """Create a new tmux window and optionally start the agent CLI.

        Args:
            work_dir: Working directory for the new window
            window_name: Optional window name (defaults to directory name)
            start_claude: Whether to launch the agent CLI (kept name for
                back-compat; applies to whichever runtime)
            resume_session_id: If set (and well-formed), resume that session
            runtime: Agent runtime to launch — "claude" (default) or "codex".
                The launch/resume command is built by runtimes.get_runtime.

        Returns:
            Tuple of (success, message, window_name, window_id)
        """
        # Validate directory first
        path = Path(work_dir).expanduser().resolve()
        if not path.exists():
            return False, f"Directory does not exist: {work_dir}", "", ""
        if not path.is_dir():
            return False, f"Not a directory: {work_dir}", "", ""

        try:
            final_window_name = await self.unique_window_name(window_name or path.name)
            sid = await asyncio.to_thread(self.ensure_session)
        except TmuxReadError as e:
            logger.error("Failed to create window: %s", e)
            return False, f"Failed to create window: {e}", "", ""

        res = await self._tmux(
            "new-window",
            "-d",
            "-t",
            f"{sid}:",
            "-n",
            final_window_name,
            "-c",
            str(path),
            "-P",
            "-F",
            "#{window_id}",
        )
        if not res.ok:
            logger.error("Failed to create window: %s", res.stderr.strip())
            return False, f"Failed to create window: {res.stderr.strip()}", "", ""
        wid = res.stdout.strip()
        if not _WINDOW_ID_RE.fullmatch(wid):
            # The -P line can be cut like any -F row (see TmuxReadError); the
            # name was just made unique, so it finds the window.
            try:
                found = await self.find_window_by_name(final_window_name)
            except TmuxReadError:
                found = None
            if found is None:
                logger.error("Created window '%s' but lost its id", final_window_name)
                return False, "Failed to create window: tmux lost its id", "", ""
            wid = found.window_id

        # Prevent Claude Code from overriding window name
        await self._tmux("set-option", "-w", "-t", wid, "allow-rename", "off")

        # Start the agent CLI if requested. The launch/resume command is
        # runtime-specific (claude: `claude --name X [--resume Y]`; codex:
        # `codex [resume Y]`) and built by the runtime — which also
        # shlex-quotes the window name and validates the resume id (an
        # unvalidated id typed into the shell is a command-injection vector).
        # (audit HIGH#1 / MEDIUM)
        if start_claude:
            cmd = get_runtime(runtime).launch_command(
                final_window_name, resume_session_id
            )
            await self.send_keys(wid, cmd, enter=False)
            await self._send_keys_once(wid, "Enter")

        logger.info("Created window '%s' (id=%s) at %s", final_window_name, wid, path)
        return (
            True,
            f"Created window '{final_window_name}' at {path}",
            final_window_name,
            wid,
        )


# Global instance with default session name
tmux_manager = TmuxManager()
