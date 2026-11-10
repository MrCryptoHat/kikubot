"""Agent runtime abstraction — Claude Code, Codex, Grok, GLM, and future CLIs.

An agent *runtime* is the CLI a topic's window runs (Claude Code, OpenAI Codex,
…). It is a second axis, independent of the *transport* (tmux vs docker — see
session.py / resolve_binding). This module isolates EVERY runtime-specific
decision behind ``AgentRuntime`` so the rest of the codebase — the monitor
especially — stays generic: it iterates runtimes and calls the interface, never
branching on ``if codex:`` at a call site (mirrors how transport lives behind
SessionManager wrappers).

Runtime is a property of the *window* (``WindowState.runtime``), NEVER encoded
in the binding string — ``@<id>`` / ``docker:<agent>`` stay transport-only, so
``resolve_binding`` is untouched. It defaults to ``"claude"`` for legacy state
(zero migration).

The interface a runtime implements:
  - ``launch_command``          — how to start/resume the CLI in a fresh pane.
  - ``iter_transcripts``        — locate the live transcript file(s) of bound
                                   windows (Claude: session_map + scan_projects;
                                   Codex: rollout resolved by cwd).
  - ``parse_entries``           — turn raw transcript lines into the shared
                                   ``ParsedEntry`` (Claude & Codex schemas differ;
                                   the output type does not).
  - ``latest_context_tokens``   — context-window fill for the context alert
                                   (runtime-specific usage shape; None to skip).
  - ``latest_usage``            — context, model and usage limits for the
                                   agent status card (agent_status.py).
  - ``list_sessions``           — resumable sessions for a directory, for the
                                   picker's per-runtime tab (Claude: project
                                   glob; Codex: rollout-by-cwd; → ``AgentSession``).

Everything downstream of ``parse_entries`` (NewMessage → queue → voice / tables
/ pins) is runtime-agnostic. Adding a third model = one ``AgentRuntime``
subclass implementing these; no monitor rewrite, and it becomes a picker tab
automatically (``pickable_runtimes()`` walks the registry).

Window-bootstrap divergence is expressed as *capabilities*, not name checks:
``uses_session_map`` (hook wait / cwd persistence / stale-window sweep),
``auto_forward_first_message``, ``ready_message_key``, ``interrupt_keys``
(/esc), ``is_available`` (picker-tab gating on an installed CLI),
``history_transcript`` (the /commands history source). Call sites must never
compare ``runtime.name``.

Key API: ``get_runtime(name)`` → AgentRuntime; module singletons ``CLAUDE`` /
``CODEX`` / ``GROK`` / ``GLM``; ``RUNTIMES`` registry; ``monitored_runtimes()`` /
``pickable_runtimes()`` (installed CLIs only) / ``default_runtime()``
(KIKUBOT_DEFAULT_RUNTIME, validated + availability-checked); ``is_valid_runtime``.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_session import AgentSession
from .codex_transcript_parser import CodexTranscriptParser
from .config import config
from .grok_transcript_parser import GrokTranscriptParser
from .terminal_parser import (
    _is_chrome_separator,
    claude_composer_text,
    claude_composer_typed,
    claude_slash_menu_pick,
    claude_trust_dialog,
    composer_holds_text,
    has_codex_queued_messages,
    has_grok_queued_messages,
    has_queued_messages,
    is_claude_working,
    is_codex_working,
    is_grok_working,
    is_interactive_ui,
    strip_escapes,
)
from .transcript_parser import ParsedEntry, TranscriptParser
from .utils import is_valid_session_id, same_dir as _same_dir

logger = logging.getLogger(__name__)

CLAUDE_RUNTIME = "claude"
# Slash commands that take Claude Code off its terminal: quit, log out,
# detach to a background session (/background, alias /bg), restart in place.
_CLAUDE_EXIT_COMMANDS = ("/exit", "/quit", "/logout", "/background", "/bg", "/restart")
CODEX_RUNTIME = "codex"
GROK_RUNTIME = "grok"
GLM_RUNTIME = "glm"

# A transcript reference the monitor tracks: (session_id, file_path).
TranscriptRef = tuple[str, Path]

# Session id embedded in a Codex rollout filename
# (rollout-<ISO-ts>-<uuid>.jsonl). Codex uses UUIDv7 (8-4-4-4-12 hex).
_CODEX_ROLLOUT_SID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

# Newest rollouts scanned when listing a cwd's codex sessions for the picker.
# Only the cheap cached first-line read runs per file; the full read (summary +
# count) runs for the ≤10 that match the cwd. Bounds a large ~/.codex tree.
_CODEX_LIST_SCAN_CAP = 150
_CODEX_LIST_MAX = 10
# Newest rollouts scanned when resolving live windows' transcripts each monitor
# tick. A window whose rollout falls outside this prefix is covered by the
# sticky ``_last_rollout`` fallback (see CodexRuntime.__init__).
_CODEX_SCAN_CAP = 60

# Sessions full-read when listing a cwd's grok sessions for the picker. Grok's
# per-cwd group directory is already the index, so only the newest N session
# dirs are read at all.
_GROK_LIST_MAX = 10

# binary name -> (login-shell `command -v` result, probe monotonic time).
# POSITIVES are cached for the process lifetime — a resolved path doesn't move,
# and the probe is a SYNCHRONOUS subprocess on the event loop (sh -l loads the
# user's profile; with nvm-style profiles that's 0.3-1 s of frozen handlers),
# so it must run at most once per binary. NEGATIVES expire after the TTL so a
# transient miss (CLI self-update swapping the binary, install-in-progress)
# self-heals without a bot restart; a genuinely-absent CLI re-probes at most
# once a minute, and only from user-driven picker renders. Only the slow
# login-shell leg is cached; the shutil.which fast path stays per-call.
_login_shell_which_cache: dict[str, tuple[str | None, float]] = {}
_LOGIN_WHICH_NEGATIVE_TTL = 60.0
# Binaries whose PATH-divergence WARNING already fired (once per process —
# the divergence is a stable host property, not a per-render event).
_login_shell_warned: set[str] = set()


def _login_shell_which(binary: str) -> str | None:
    """``command -v`` under a login shell, cached per binary name.

    Kept as a separate seam so tests can stub it out — the real probe would
    find the host's actual CLIs and defeat availability-gating assertions.
    """
    cached = _login_shell_which_cache.get(binary)
    if cached is not None and (
        cached[0] is not None
        or time.monotonic() - cached[1] < _LOGIN_WHICH_NEGATIVE_TTL
    ):
        return cached[0]
    path: str | None = None
    try:
        proc = subprocess.run(
            ["sh", "-lc", f"command -v {shlex.quote(binary)}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Judge the LAST stdout line: a chatty ~/.profile may print banners
        # before command -v's answer, and testing the whole stdout against
        # startswith("/") would discard a perfectly good resolution.
        last = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout else ""
        if proc.returncode == 0 and last.startswith("/"):
            path = last
    except (OSError, subprocess.SubprocessError):
        path = None
    if path is not None and binary not in _login_shell_warned:
        # which() missed but the login shell resolves it — the PATH divergence
        # this fallback exists for. Logged ONCE so a live process shows WHY a
        # picker tab would have vanished (2026-07-23: claude in ~/.npm-global).
        _login_shell_warned.add(binary)
        logger.warning(
            "CLI %r not on the bot's PATH; resolved via login shell to %s",
            binary,
            path,
        )
    _login_shell_which_cache[binary] = (path, time.monotonic())
    return path


def _resolve_cli_binary(binary: str) -> str | None:
    """Absolute path of an agent CLI binary, or None when not installed.

    ``shutil.which`` first (the bot process's PATH), then a one-time cached
    login-shell probe. The second leg is load-bearing, not a nicety: the bot
    runs under systemd/tmux with a minimal PATH, while the CLI may live in a
    login-shell-only dir (``~/.npm-global/bin``, nvm) — the PANES that launch
    the CLI run the user's login shell, so its resolution is the truth. With
    which() alone, claude's picker tab silently vanished and its version
    canary went dark on exactly such a host, while claude windows kept
    launching fine.
    """
    binary = os.path.expanduser(binary)
    found = shutil.which(binary)
    if found:
        return found
    if os.path.isabs(binary):
        return None  # explicit path that doesn't exist — nothing to probe
    return _login_shell_which(binary)


def _entry_context_tokens(entry: dict) -> int | None:
    """Context-window fill (input-side tokens) for a Claude assistant entry.

    Returns input + cache_creation + cache_read tokens — the prompt size Claude
    Code reports as "X/1m" in /context. Output tokens are the *next* turn's
    input, so excluded to match. None for non-assistant entries or ones with no
    usage block. (Lives here, not in the monitor, so the extraction is behind
    the runtime; the threshold bookkeeping stays generic in the monitor.)
    """
    if entry.get("type") != "assistant":
        return None
    usage = (entry.get("message") or {}).get("usage") or {}
    if not usage:
        return None
    return (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )


@dataclass(frozen=True)
class UsageLimit:
    """One usage-limit window as an agent CLI reports it."""

    id: str  # "5h", "7d", or the CLI's own name for an odd window
    pct_used: float
    resets_at: int | None = None  # unix seconds
    window_minutes: int | None = None


@dataclass(frozen=True)
class AgentUsage:
    """What a transcript batch says about the session (None = not reported)."""

    context_tokens: int | None = None
    context_window: int | None = None
    model: str | None = None  # the model that actually answered, CLI's id
    limits: tuple[UsageLimit, ...] = ()
    # Files the agent's edit tools touched in this batch (as the CLI wrote them).
    edited_files: tuple[str, ...] = ()
    # Subagent lifecycle, in order: ("start", key, description) / ("done", key, "").
    agent_events: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class ExitPrompt:
    """A screen an agent CLI drew on its exit command instead of quitting.

    ``background``: running work would stop; ``items`` lists it as the CLI
    does, and ``stop_selected`` says the highlighted answer is «stop it and
    exit». ``notice``: an informational screen that Esc closes, after which
    the exit goes on.
    """

    kind: str  # "background" | "notice"
    items: tuple[str, ...] = ()
    stop_selected: bool = False


_WINDOW_IDS = {300: "5h", 10080: "7d"}

# A subagent the Agent tool launched in the background reports back later in
# a <task-notification> carrying the launching tool_use id and its status.
_NOTIFY_RE = re.compile(
    r"<tool-use-id>([^<]+)</tool-use-id>.*?<status>(completed|failed|killed|stopped|cancelled)</status>",
    re.DOTALL,
)
_CLAUDE_AGENT_TOOLS = frozenset({"Agent", "Task"})
# How Claude Code opens the tool_result of a subagent it runs in the
# background (no run_in_background flag needed: 2.1.3xx launches them async by
# default). Any other result is a foreground subagent's report — it is done.
_ASYNC_AGENT_PREFIX = "Async agent launched"


def _block_text(content: object) -> str:
    """Text of a message/tool_result content (a str or a list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _claude_activity(
    raw_entries: list[dict], edit_tools: frozenset[str]
) -> tuple[tuple[str, ...], tuple[tuple[str, str, str], ...]]:
    """(edited files, subagent events) in a batch of Claude transcript entries."""
    files: list[str] = []
    events: list[tuple[str, str, str]] = []
    for raw in raw_entries:
        content = (raw.get("message") or {}).get("content")
        if raw.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                inp = b.get("input")
                if not isinstance(inp, dict):
                    inp = {}
                if b.get("name") in edit_tools:
                    path = inp.get("file_path") or inp.get("notebook_path")
                    if isinstance(path, str) and path:
                        files.append(path)
                elif b.get("name") in _CLAUDE_AGENT_TOOLS and isinstance(
                    b.get("id"), str
                ):
                    desc = inp.get("description") or inp.get("subagent_type") or ""
                    events.append(("start", b["id"], str(desc)))
        elif raw.get("type") == "user":
            if isinstance(content, list):
                for b in content:
                    # Any tool's result may close a subagent: a foreground
                    # one answers minutes later, in another monitor batch,
                    # so it can't be matched to a launch seen in this one.
                    # The cache ignores ids it doesn't hold.
                    if (
                        isinstance(b, dict)
                        and b.get("type") == "tool_result"
                        and isinstance(b.get("tool_use_id"), str)
                        and not _block_text(b.get("content"))
                        .lstrip()
                        .startswith(_ASYNC_AGENT_PREFIX)
                    ):
                        events.append(("done", b["tool_use_id"], ""))
            text = _block_text(content)
            if "<task-notification>" in text:
                for m in _NOTIFY_RE.finditer(text):
                    events.append(("done", m.group(1), ""))
    return tuple(files), tuple(events)


# Claude Code's footer under the input box: «⏵⏵ auto mode on · 2 shells · …».
# No mode marker there means the default (ask before changes).
_CLAUDE_MODE_RE = re.compile(
    r"(?:⏵⏵|⏸)\s+(auto mode|plan mode|accept edits|bypass permissions) on"
)
_CLAUDE_MODES = {
    "auto mode": "auto",
    "plan mode": "plan",
    "accept edits": "accept_edits",
    "bypass permissions": "bypass",
}
_CLAUDE_SHELLS_RE = re.compile(r"(\d+) shells?\b")


_EXIT_OPTION_RE = re.compile(r"^(❯\s*)?\d+\.\s")


def _claude_exit_prompt(pane_text: str) -> ExitPrompt | None:
    """What Claude Code (2.1.31x+) drew on /exit instead of quitting.

    Matched only as the block that owns the bottom of the pane, below the
    last chrome rule and ending in its key hint: the same words turn up
    quoted in transcripts, and a match there would stall every restart.
    """
    lines = [ln.strip() for ln in pane_text.split("\n")]
    seps = [i for i, ln in enumerate(lines) if _is_chrome_separator(ln)]
    if not seps:
        return None
    block = [ln for ln in lines[seps[-1] + 1 :] if ln]
    if len(block) < 2:
        return None
    head, footer = block[0], block[-1]
    if head == "Background work is running" and "Esc to cancel" in footer:
        items: list[str] = []
        listing = stop = False
        for ln in block[1:-1]:
            if _EXIT_OPTION_RE.match(ln):
                listing = False
                if ln.startswith("❯"):
                    stop = "stop" in ln.lower()
            elif listing:
                items.append(ln)
            elif ln.endswith("will stop when you exit:"):
                listing = True
        return ExitPrompt("background", tuple(items), stop)
    if head == "Feedback drafts" and "Esc to close" in footer:
        return ExitPrompt("notice")
    return None


def _claude_footer(pane_text: str) -> str | None:
    """The lines below Claude Code's input box, or None when none is drawn.

    The box is the region between the last two chrome rules and starts with
    the ❯ prompt; under it come the user's own status line (if any) and then
    the mode / shells line, so every line there is searched. A dialog or a
    shell owning the pane has no box — None, so nothing reads it as a mode.
    """
    if not pane_text or is_interactive_ui(pane_text):
        return None
    lines = pane_text.split("\n")
    seps = [
        i
        for i in range(max(0, len(lines) - 40), len(lines))
        if _is_chrome_separator(lines[i].strip())
    ]
    if len(seps) < 2:
        return None
    box = lines[seps[-2] + 1 : seps[-1]]
    if not box or box[0].lstrip()[:1] not in ("❯", ">", "›"):
        return None
    return "\n".join(ln for ln in lines[seps[-1] + 1 :] if ln.strip())


def limit_id(window_minutes: int | None, fallback: str) -> str:
    """Stable id of a limit window: "5h"/"7d" for the common ones."""
    if window_minutes in _WINDOW_IDS:
        return _WINDOW_IDS[window_minutes]
    return f"{window_minutes}m" if window_minutes else fallback


class AgentRuntime(abc.ABC):
    """One agent CLI's runtime-specific behavior.

    The base class is abstract; use the concrete singletons below (``CLAUDE`` /
    ``CODEX``) and resolve via :func:`get_runtime`. ``session_manager`` /
    ``monitor`` are passed to ``iter_transcripts`` as objects (typed ``Any`` to
    avoid an import cycle — the monitor imports this module).
    """

    #: Stable identifier persisted in ``WindowState.runtime`` and env config.
    name: str = ""
    #: Short label for UI (the runtime picker, status). Bilingual-neutral —
    #: these are product names, not translated strings.
    display_name: str = ""
    #: Whether this runtime's CLI registers sessions via a SessionStart hook
    #: into a session_map (Claude Code). Gates the window-bootstrap behavior at
    #: EVERY creation path (bot._create_and_bind_window, worktree provision,
    #: auto-rebind): a session_map runtime waits for the hook entry and raises
    #: the hook-missing alarm; a runtime WITHOUT one (Codex) skips the wait and
    #: instead needs ``WindowState.cwd`` persisted so the monitor can resolve
    #: its transcript by cwd. Also gates load_session_map's stale-window sweep
    #: (only session_map windows may be reaped for being absent from the map).
    #: Never branch on the runtime *name* for any of this — a third runtime
    #: with its own hook must not silently inherit Codex's treatment.
    uses_session_map: bool = True
    #: Whether the first (pending) topic message may be auto-typed into a
    #: freshly created window. False for CLIs that can open on an interactive
    #: startup screen (Codex's sign-in menu) where blind typing + Enter would
    #: take a step the user didn't choose.
    auto_forward_first_message: bool = True
    #: Seconds to wait after launch before auto-typing that first message.
    #: Claude needs none (its hook wait already spaces the forward); a
    #: hookless runtime that DOES auto-forward (Grok) declares its boot time
    #: here explicitly instead of relying on incidental awaits to cover it —
    #: keys typed into a still-booting TUI can be swallowed.
    first_message_settle_sec: float = 0.0
    #: Keys sent by /esc to interrupt a running turn, in order (tmux key
    #: names). Claude Code takes Escape (agent) + Ctrl-C (bash command);
    #: Codex must NOT get the trailing Ctrl-C — on its TUI an idle Ctrl-C arms
    #: the quit sequence instead of being a no-op.
    interrupt_keys: tuple[str, ...] = ("Escape", "C-c")
    #: Emoji shown next to ``display_name`` on the picker's runtime tab (inactive
    #: tab: ``{icon} {display_name}``; active tab swaps it for a ``▸`` pointer,
    #: matching the agent-panel tab convention). A new runtime just sets its own.
    picker_icon: str = ""

    #: Which of the agent panel's *divergent* buttons this runtime offers, by
    #: logical action id. The panel shows a gated button only when its id is
    #: here (``supports_panel_action``), so the keyboard reflects the agent's
    #: real capabilities instead of assuming Claude Code's command set — no
    #: ``if codex:`` at the call site. Universal keys (esc/arrows/slash/enter/
    #: wipe) and lifecycle (resume/new/restart/end/refresh) are always shown and
    #: NOT listed here. Ids: "mode" (Shift+Tab plan/auto-accept cycle), "effort",
    #: "compact", "clear", "model", "context", "mcp", "background" (Ctrl+B →
    #: background task), "rewind" (/rewind). 🌳 worktrees are not a runtime
    #: action: the bot makes them (SessionManager.can_offer_worktree). A third agent = a new
    #: subclass declaring its own set; the panel builder never changes.
    panel_actions: frozenset[str] = frozenset()

    #: Logical panel-action id → the slash command typed into the pane for that
    #: button. The base map is Claude Code's; a runtime whose command differs
    #: overrides just that entry (e.g. Codex "context" → ``/status``), so the
    #: callback handler resolves the real command through the runtime instead of
    #: a global Claude-shaped table. Actions driven by a KEY (mode = Shift+Tab)
    #: or a lifecycle handler (resume) aren't listed here.
    panel_slash_commands: dict[str, str] = {
        "clear": "/clear",
        "compact": "/compact",
        "model": "/model",
        "mcp": "/mcp",
        "context": "/context",
        "effort": "/effort",
        "resume": "/resume",
        "rewind": "/rewind",
    }

    def supports_panel_action(self, action: str) -> bool:
        """True iff this runtime offers the given gated panel button."""
        return action in self.panel_actions

    def ready_message_key(self, resumed: bool) -> str:
        """i18n key of the "window is ready" message posted after creation.

        ``resumed`` distinguishes a fresh window from a ``--resume`` one. A
        runtime with a startup screen the user must know about (Codex sign-in)
        overrides this to its own key for both cases.
        """
        return "bot.window_resumed" if resumed else "bot.window_ready"

    @abc.abstractmethod
    def cli_command(self) -> str:
        """The configured shell command that launches this runtime's CLI.

        Only the first token is used for availability probing; the full string
        is what ``launch_command`` builds on.
        """
        ...

    def is_available(self) -> bool:
        """True iff this runtime's CLI binary is installed.

        Gates the picker tabs (``pickable_runtimes``): a runtime whose binary
        is absent must not be offered — typing a missing command into a fresh
        pane leaves a dead window the health check reaps 30 s later, with no
        explanation the user can act on. Resolution goes through
        ``_resolve_cli_binary`` — the bot process's PATH alone is NOT the
        truth (see that helper: a claude living in ``~/.npm-global/bin``
        vanished from the picker while its windows launched fine).
        """
        parts = self.cli_command().split()
        if not parts:
            return False
        return _resolve_cli_binary(parts[0]) is not None

    async def cli_version(self) -> str | None:
        """First line of ``<cli> --version`` output (None if the probe fails).

        Feeds the self-update canary (status_polling's version watcher): agent
        CLIs update themselves in place — codex does so silently — and every
        TUI anchor (busy counter, /status box, diff crop, menu chrome) is
        pinned to the version its patterns were captured on. A version bump is
        the moment those anchors need re-verification, so it must not pass
        unnoticed.
        """
        parts = self.cli_command().split()
        if not parts:
            return None
        binary = _resolve_cli_binary(parts[0])
        if binary is None:
            # Un-resolvable binary (also under the service PATH divergence
            # _resolve_cli_binary covers) — a relative exec would fail anyway.
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except (OSError, asyncio.TimeoutError):
            return None
        lines = out.decode(errors="replace").strip().splitlines()
        return lines[0].strip() if lines else None

    def panel_slash(self, action: str) -> str | None:
        """Slash command this runtime types for a panel action (None if none)."""
        return self.panel_slash_commands.get(action)

    #: /diff (opt-in edit-diff screenshots). ``edit_tool_names`` are the tool_use
    #: names whose activity triggers a pane scan; ``diff_header_re`` matches the
    #: line that STARTS the runtime's native diff block and ``diff_boundary_re``
    #: a line that ENDS it (besides a blank line). Empty / None → the runtime
    #: renders no croppable diff and /diff no-ops for it. diff_view owns the
    #: shared crop engine and reads these (kept as data here to avoid a runtimes
    #: → handlers import cycle).
    edit_tool_names: frozenset[str] = frozenset()
    diff_header_re: re.Pattern[str] | None = None
    diff_boundary_re: re.Pattern[str] | None = None

    def is_edit_tool(self, name: str | None) -> bool:
        """True iff a tool_use with this name should trigger a /diff scan."""
        return bool(name) and name in self.edit_tool_names

    #: How the runtime ingests an inbound image. False (default): a text marker
    #: ``(image attached: <path>)`` is typed and the CLI reads the file itself
    #: (Claude Code). True: the CLI has NATIVE multimodal input — the path is
    #: typed into its composer, which attaches the image CLIENT-side (Codex →
    #: ``[Image #N]``). The client-side read is what makes it work under codex's
    #: sandbox, which blocks reading a path outside the workspace (the bug this
    #: fixes). ``composer_image_token`` is the pane text that confirms the
    #: attach landed, polled by ``session_manager.send_composer_image``.
    native_image_input: bool = False
    composer_image_token: str = ""

    def image_marker(self, path: str) -> str:
        """Text-marker form for a text-marker runtime (native_image_input=False)."""
        return f"(image attached: {path})"

    async def list_sessions(self, session_manager: Any, cwd: str) -> list[AgentSession]:
        """Resumable sessions this runtime has for ``cwd`` (newest first, capped).

        Powers the picker's per-runtime session tab: the user picks Claude Code
        or Codex on top, and this returns *that* runtime's resume candidates for
        the folder. Default: none (a runtime with no resumable transcript store).
        ``session_manager`` is passed as ``Any`` to avoid an import cycle.
        """
        return []

    async def history_transcript(
        self, session_manager: Any, window_id: str
    ) -> Path | None:
        """Path of the window's current transcript for the history view.

        Powers ``get_recent_messages`` (the /commands history pages and unread
        catch-up) — runtime-dispatched because each CLI stores transcripts
        differently. Default: the Claude session_id+cwd JSONL resolution (also
        correct for docker bindings via their projects root).
        """
        session = await session_manager.resolve_session_for_window(window_id)
        if session and session.file_path:
            return Path(session.file_path)
        return None

    @abc.abstractmethod
    def launch_command(
        self, window_name: str, resume_session_id: str | None = None
    ) -> str:
        """Shell command to type into a fresh pane to start (or resume) the agent.

        ``window_name`` is the tmux window/display name; ``resume_session_id``
        resumes a prior session when set and well-formed (a malformed id is
        ignored and a fresh session starts — an unvalidated id is a shell-
        injection vector, so callers must never bypass this).
        """
        ...

    @abc.abstractmethod
    async def iter_transcripts(
        self, session_manager: Any, monitor: Any, active_session_ids: set[str]
    ) -> list[TranscriptRef]:
        """Return ``(session_id, file_path)`` for each live transcript to read.

        Called once per monitor tick. ``active_session_ids`` is the set from the
        (Claude) session_map; runtimes without a session_map (Codex) ignore it
        and resolve by their own means.
        """
        ...

    @abc.abstractmethod
    def parse_entries(
        self, raw_entries: list[dict], pending_tools: dict
    ) -> tuple[list[ParsedEntry], dict]:
        """Parse raw transcript lines into ParsedEntry + carry-state.

        ``pending_tools`` is an OPAQUE per-session dict the monitor hands back
        on the next batch — tool_use↔tool_result pairing for Claude/Codex, but
        a runtime may stash any cross-batch parser state in it (Grok carries a
        still-streaming chunk buffer under a reserved key). Consumers must not
        assume every value is a pending-tool record.
        """
        ...

    def latest_context_tokens(self, raw_entries: list[dict]) -> int | None:
        """Latest context-window fill across ``raw_entries`` (None → no alert).

        Default: no context tracking. Runtimes with a usage block override.
        """
        return None

    def latest_usage(self, raw_entries: list[dict]) -> AgentUsage | None:
        """Context, model and limits ``raw_entries`` report (None → nothing).

        Feeds the agent status card. Default: the context fill alone.
        """
        tokens = self.latest_context_tokens(raw_entries)
        return AgentUsage(context_tokens=tokens) if tokens is not None else None

    def permission_mode(self, pane_text: str) -> str | None:
        """The permission mode the pane's footer shows, as a stable id.

        For the agent status card. Default: unknown (None).
        """
        return None

    def background_shells(self, pane_text: str) -> int | None:
        """How many background shells the pane's footer reports (None: unknown)."""
        return None

    def has_transcript(self, session_id: str) -> bool | None:
        """Whether the CLI has a transcript to resume ``session_id`` from.

        A resume of a session that never got one (started, then restarted
        before its first message) makes the CLI quit at once, and a revive
        loop follows. None = can't tell; callers then resume as asked.
        """
        return None

    def typed_input(self, pane_ansi: str) -> str | None:
        """What is typed in the input box, from ``capture-pane -e``.

        A suggestion the CLI draws into an empty box doesn't count. "" is an
        empty box; None means the box can't be read (the default), and callers
        that must know (``SessionManager.clear_input``) then don't act blind.
        """
        return None

    def clear_input_pass(self, pane_text: str) -> tuple[str, ...]:
        """Keys for one pass of emptying the input box; () = no known way.

        Must never interrupt a running turn, so no Esc or Ctrl-C.
        ``SessionManager.clear_input`` repeats the pass until ``typed_input``
        reads empty.
        """
        return ()

    def would_exit_on_enter(self, pane_text: str) -> bool:
        """Whether Enter now would quit the CLI.

        The inline ``keys`` query refuses such an Enter, so a client can't end
        the agent and leave its next keys to the shell. False when the CLI's
        screen can't be read that way (the keys path still checks typed text
        against ``exit_command``).
        """
        return False

    def would_exit_on_escape(self, pane_text: str) -> bool:
        """Whether Esc now would quit the CLI (a startup gate it cancels)."""
        return False

    def exit_prompt(self, pane_text: str) -> ExitPrompt | None:
        """A screen the CLI drew on ``exit_command`` instead of quitting.

        The restart reads it so it never confirms one blind: Enter there takes
        whatever is preselected, and Claude Code preselects «stop background
        work». Default: the CLI has no such screen.
        """
        return None

    def remote_control(self, pane_text: str) -> str | None:
        """ "connected" / "available" / None (off or unknown) off the pane."""
        return None

    # Permission modes `/mode` can switch between (ids as permission_mode
    # returns them); () = no modes.
    permission_modes: tuple[str, ...] = ()

    def infer_context_window(self, used_tokens: int) -> int | None:
        """The context window size when nothing reports it (None: unknown)."""
        return None

    def pretty_model(self, model_id: str) -> str:
        """A model id as a person reads it. Default: as it is."""
        return model_id

    def configured_effort(self, model_id: str | None) -> str | None:
        """The effort level the CLI is set to use for ``model_id`` (None: unknown)."""
        return None

    # Model aliases the CLI's `/model <alias>` accepts, (alias, label), for the
    # status sheet's model picker; () = no picker, `/model` opens the CLI's own.
    model_aliases: tuple[tuple[str, str], ...] = ()
    # Levels `/effort <level>` accepts; () = no effort picker.
    effort_levels: tuple[str, ...] = ()

    # Launched with kikubot's status-line tap (`kikubot statusline`) when
    # KIKUBOT_USAGE_LIMITS is on — Claude Code only: the tap reads its stdin.
    statusline_tap = False

    def is_working(self, pane_text: str) -> bool:
        """True iff the pane shows a turn actively running (interruptible).

        Gates every "don't barge the agent" decision (restart guard, /inject
        busy-gate, task-pin idle check, reaction-confirm). Default is Claude
        Code's status-line detection; a runtime with different TUI chrome
        (Codex) overrides. Behind the runtime, NOT an ``if codex:`` at each call
        site — call sites go through ``session_manager.is_agent_working``.
        """
        return is_claude_working(pane_text)

    def unsent_in_composer(self, pane_text: str, sent: str) -> bool:
        """True iff typed ``sent`` still sits unsubmitted in the input box.

        Checked after every typed message's Enter; True makes the sender press
        Enter again (``SessionManager._confirm_submitted``). Default reads
        Claude Code's box; a runtime whose box it can't read returns False,
        which skips the check rather than re-pressing Enter blind.
        """
        return composer_holds_text(pane_text, sent)

    def has_queued_input(self, pane_text: str) -> bool:
        """True iff the CLI shows buffered, not-yet-ingested input.

        Drives reaction-ack timing: the 👀 fires when this flips False (the
        queued message entered the agent's context). Default is Claude Code's
        queued-messages hint; Codex overrides with its own.
        """
        return has_queued_messages(pane_text)

    def exit_command(self) -> str:
        """Text typed (+ Enter) to quit the CLI's TUI back to the shell.

        Used by the restart handlers (exit → relaunch in the same pane).
        Default is Claude Code's ``/exit``; Codex overrides.
        """
        return "/exit"


class ClaudeRuntime(AgentRuntime):
    """Claude Code — the legacy/default runtime."""

    name = CLAUDE_RUNTIME
    display_name = "Claude Code"
    picker_icon = "🟠"
    # /diff: Claude Code renders "● Update/Write(path)" blocks with numbered
    # ±gutter lines; a block ends at the next tool bullet (● / ⏺).
    edit_tool_names = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
    diff_header_re = re.compile(
        r"^[●⏺]\s+(Update|Write|Create|Edit|MultiEdit|NotebookEdit)\("
    )
    diff_boundary_re = re.compile(r"^[●⏺]")
    # Claude Code offers the full panel — this is the historical baseline.
    panel_actions = frozenset(
        {
            "mode",
            "effort",
            "compact",
            "clear",
            "model",
            "context",
            "mcp",
            "background",
            "rewind",
        }
    )

    def cli_command(self) -> str:
        return config.claude_command

    statusline_tap = True
    permission_modes = ("manual", "accept_edits", "plan", "auto")
    model_aliases = (
        ("opus", "Opus"),
        ("fable", "Fable"),
        ("sonnet", "Sonnet"),
        ("haiku", "Haiku"),
    )
    # `claude --help`: --effort <level> (low, medium, high, xhigh, max). The
    # slash form SAVES the level for that model in ~/.claude/settings.json, so
    # it reaches every session on the machine, not just this one.
    effort_levels = ("low", "medium", "high", "xhigh", "max")

    def infer_context_window(self, used_tokens: int) -> int | None:
        # Claude's windows are 200k or 1M: a fill past 200k can only be 1M.
        return 1_000_000 if used_tokens > 200_000 else None

    def configured_effort(self, model_id: str | None) -> str | None:
        # Claude Code keeps it in ~/.claude/settings.json: per model under
        # modelSettings (what `/effort` writes), else the global effortLevel.
        from .hook import _claude_settings_file

        try:
            data = json.loads(_claude_settings_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        per_model = data.get("modelSettings")
        if model_id and isinstance(per_model, dict):
            entry = per_model.get(model_id.split("[", 1)[0])
            level = entry.get("effortLevel") if isinstance(entry, dict) else None
            if isinstance(level, str):
                return level
        level = data.get("effortLevel")
        return level if isinstance(level, str) else None

    def pretty_model(self, model_id: str) -> str:
        # «claude-opus-5-5[1m]» → «Opus 5.5», the way the status line names it.
        m = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d+))?(?:\[.*\])?", model_id)
        if not m:
            return model_id
        version = m.group(2) + (f".{m.group(3)}" if m.group(3) else "")
        return f"{m.group(1).title()} {version}"

    def permission_mode(self, pane_text: str) -> str | None:
        footer = _claude_footer(pane_text)
        if footer is None:
            return None
        m = _CLAUDE_MODE_RE.search(footer)
        return _CLAUDE_MODES[m.group(1)] if m else "manual"

    def remote_control(self, pane_text: str) -> str | None:
        # A connected session labels the input box's top rule «/rc active»;
        # with Remote Control merely enabled, the footer ends in «/rc».
        tail = pane_text.split("\n")[-40:]
        if any("/rc active" in ln and _is_chrome_separator(ln.strip()) for ln in tail):
            return "connected"
        footer = _claude_footer(pane_text)
        if footer and any(ln.rstrip().endswith("/rc") for ln in footer.split("\n")):
            return "available"
        return None

    def has_transcript(self, session_id: str) -> bool | None:
        # Any project dir: a resume from another cwd copies the session there.
        if not is_valid_session_id(session_id):
            return None
        try:
            return any(config.claude_projects_path.glob(f"*/{session_id}.jsonl"))
        except OSError:
            return None

    def typed_input(self, pane_ansi: str) -> str | None:
        return claude_composer_typed(pane_ansi)

    def clear_input_pass(self, pane_text: str) -> tuple[str, ...]:
        # Measured on 2.1.322: Ctrl+K kills to the line end (at the end, the
        # line break), Ctrl+U to the line start (at the start, the break
        # before). Alternating them empties a multi-line draft from any cursor
        # position in about two pairs per line; one call types them all.
        box = claude_composer_text(strip_escapes(pane_text)) or ""
        rows = box.count("\n") + 1
        return ("C-k", "C-u") * min(2 * rows + 2, 80)

    def would_exit_on_enter(self, pane_text: str) -> bool:
        plain = strip_escapes(pane_text)
        _, exit_picked = claude_trust_dialog(plain)
        if exit_picked:
            return True  # the folder-trust gate with «No, exit» highlighted
        if claude_slash_menu_pick(plain) in _CLAUDE_EXIT_COMMANDS:
            return True  # Enter runs the slash menu's highlighted command
        box = claude_composer_text(plain)
        if not box:
            return False
        text = box.replace("❯", "").replace("\xa0", " ").strip().lower()
        if text in ("exit", "quit"):
            return True
        if not text.startswith("/"):
            return False
        first = text.split()[0]
        # Enter completes a partial slash command: "/ex" runs /exit.
        return any(
            first == cmd or (len(text) >= 2 and cmd.startswith(text))
            for cmd in _CLAUDE_EXIT_COMMANDS
        )

    def would_exit_on_escape(self, pane_text: str) -> bool:
        trust_up, _ = claude_trust_dialog(pane_text)
        return trust_up  # Esc cancels the trust gate, and the CLI quits

    def exit_prompt(self, pane_text: str) -> ExitPrompt | None:
        return _claude_exit_prompt(pane_text)

    def background_shells(self, pane_text: str) -> int | None:
        footer = _claude_footer(pane_text)
        if footer is None:
            return None
        m = _CLAUDE_SHELLS_RE.search(footer)
        return int(m.group(1)) if m else 0

    def _claude_invocation(self) -> str:
        """The ``claude`` command line that ``launch_command`` extends."""
        if config.usage_limits_enabled:
            from .agent_status import write_statusline_settings

            path = write_statusline_settings()
            if path is not None:
                return f"{config.claude_command} --settings {shlex.quote(str(path))}"
        return config.claude_command

    def launch_command(
        self, window_name: str, resume_session_id: str | None = None
    ) -> str:
        # shlex.quote (not repr): this string is typed into the pane's shell,
        # and repr() is not shell-safe for a name holding both quote kinds.
        cmd = f"{self._claude_invocation()} --name {shlex.quote(window_name)}"
        if resume_session_id and is_valid_session_id(resume_session_id):
            cmd = f"{cmd} --resume {resume_session_id}"
        elif resume_session_id:
            logger.warning(
                "Ignoring malformed resume session id for window %s; starting fresh",
                window_name,
            )
        return cmd

    async def iter_transcripts(
        self, session_manager: Any, monitor: Any, active_session_ids: set[str]
    ) -> list[TranscriptRef]:
        # scan_projects walks every project root (host + docker bind-mounts);
        # the session_map (active_session_ids) filters to sessions the hook
        # actually registered — exactly the pre-unification behavior.
        sessions = await monitor.scan_projects()
        return [
            (s.session_id, s.file_path)
            for s in sessions
            if s.session_id in active_session_ids
        ]

    def parse_entries(
        self, raw_entries: list[dict], pending_tools: dict
    ) -> tuple[list[ParsedEntry], dict]:
        return TranscriptParser.parse_entries(raw_entries, pending_tools=pending_tools)

    async def list_sessions(self, session_manager: Any, cwd: str) -> list[AgentSession]:
        # The Claude enumeration already lives on SessionManager (globs
        # ~/.claude/projects/<encoded cwd>/*.jsonl); reuse it verbatim.
        return await session_manager.list_sessions_for_directory(cwd)

    def latest_context_tokens(self, raw_entries: list[dict]) -> int | None:
        latest: int | None = None
        for raw in raw_entries:
            t = _entry_context_tokens(raw)
            if t is not None:
                latest = t
        return latest

    def latest_usage(self, raw_entries: list[dict]) -> AgentUsage | None:
        files, events = _claude_activity(raw_entries, self.edit_tool_names)
        tokens: int | None = None
        model: str | None = None
        for raw in raw_entries:
            t = _entry_context_tokens(raw)
            if t is not None:
                tokens = t
                m = (raw.get("message") or {}).get("model")
                if isinstance(m, str) and m and not m.startswith("<"):
                    model = m
        if tokens is None and model is None and not files and not events:
            return None
        return AgentUsage(
            context_tokens=tokens, model=model, edited_files=files, agent_events=events
        )


class CodexRuntime(AgentRuntime):
    """OpenAI Codex CLI.

    Launch: no ``--name`` flag (the tmux window name is set at ``new_window``
    time), resume is ``codex resume <uuid>``. A bare unauthenticated ``codex``
    opens an in-TUI sign-in menu, driven by the shared interactive-UI photo +
    keys (terminal_parser CodexSignIn/CodexLogin patterns).

    Transcript: Codex has no SessionStart-hook session_map, so a window's
    rollout (``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``) is located by
    matching its cwd to a rollout's ``session_meta.cwd`` (newest wins). The
    rollout's session id is mirrored into ``WindowState.session_id`` so the
    normal find_users_for_session routing delivers replies to the topic.
    """

    name = CODEX_RUNTIME
    display_name = "Codex"
    picker_icon = "🔵"
    # No SessionStart hook in codex 0.144.x interactive launch — windows are
    # tracked by cwd→rollout matching, so bootstrap must persist cwd and skip
    # the claude session_map wait/alarm (see the base-class capability doc).
    uses_session_map = False
    # A fresh codex can open on its sign-in menu; blind-typing the pending
    # topic message (with Enter) would pick a menu option the user never chose.
    auto_forward_first_message = False
    # Idle Ctrl-C arms codex's quit sequence — /esc sends Escape only.
    interrupt_keys = ("Escape",)
    # Codex panel set (probed live). Same wire strings as Claude for
    # compact/clear/model/mcp; "mode" reuses Shift+Tab (codex cycles "Plan
    # mode" on back-tab, same key handler); "context" maps to /status (codex has
    # no /context — /status shows model/permissions/account/limits, parsed by
    # codex_status_parser). EXCLUDED: "effort" (folded into /model), "background"
    # (no Ctrl+B background-task), "rewind" (no /rewind).
    panel_actions = frozenset({"compact", "clear", "model", "mcp", "mode", "context"})
    panel_slash_commands = {
        **AgentRuntime.panel_slash_commands,
        "context": "/status",  # codex's session-status command (no /context)
    }
    # /diff: codex edits via the apply_patch tool (logged as a custom_tool_call,
    # which codex_transcript_parser now emits as a tool_use). Its native pane
    # block is "• Edited <file> (+N -M)" + numbered ±gutter lines, ended by the
    # next • bullet or the long ─ separator codex draws after a tool block.
    edit_tool_names = frozenset({"apply_patch"})
    diff_header_re = re.compile(r"^[•●]\s+\w+.*\(\+\d+")
    diff_boundary_re = re.compile(r"^[•●]|^─{5,}")
    # Codex has native multimodal input: typing an image path into the composer
    # auto-attaches it as "[Image #N]" (a client-side read that bypasses the
    # sandbox). See session_manager.send_composer_image + media.photo_handler.
    native_image_input = True
    composer_image_token = "[Image #"

    def __init__(self) -> None:
        # rollout path -> its session_meta.cwd (immutable per file); avoids
        # re-reading a rollout header every tick. Instance state on the CODEX
        # singleton.
        self._meta_cwd: dict[str, str | None] = {}
        # cwd -> last successfully resolved rollout path. Sticky fallback: the
        # mtime scan is capped (_CODEX_SCAN_CAP), so on a host with many codex
        # sessions across other projects a quiet window's rollout can fall out
        # of the scanned prefix — without this, its topic silently stops
        # receiving replies. The cached path is re-checked (exists + cwd still
        # matches) before use.
        self._last_rollout: dict[str, Path] = {}

    def cli_command(self) -> str:
        return config.codex_command

    def ready_message_key(self, resumed: bool) -> str:
        # Fresh AND resumed codex windows may open on the sign-in menu — the
        # ready message explains the nav-keys flow either way.
        return "bot.window_codex_ready"

    def launch_command(
        self, window_name: str, resume_session_id: str | None = None
    ) -> str:
        codex = config.codex_command
        # Opt-in (config.codex_bypass_sandbox): run codex unsandboxed. Needed
        # where codex's bundled bubblewrap can't initialize (nested-userns UID
        # mapping blocked) — otherwise codex can't run shell / edit files / read
        # documents. Accepted as an option by both `codex` and `codex resume`.
        flag = (
            " --dangerously-bypass-approvals-and-sandbox"
            if config.codex_bypass_sandbox
            else ""
        )
        if resume_session_id and is_valid_session_id(resume_session_id):
            return f"{codex} resume {resume_session_id}{flag}"
        elif resume_session_id:
            logger.warning(
                "Ignoring malformed resume session id for codex window %s; "
                "starting fresh",
                window_name,
            )
        # Bare `codex`: an unauthenticated launch opens codex's own in-TUI
        # sign-in MENU (ChatGPT / Device Code / API key). We keep the menu
        # (rather than forcing a flow) so the user picks the method — it's
        # detected as an interactive UI (terminal_parser CodexSignIn/CodexLogin
        # patterns) and driven by the SAME photo + ↑↓⏎ nav keyboard + auth-URL
        # surfacing Claude Code's /login uses. Nothing is auto-typed into it
        # (bot.py skips forwarding the first message to codex), so no keypress
        # mis-selects an option.
        return f"{codex}{flag}"

    def parse_entries(
        self, raw_entries: list[dict], pending_tools: dict
    ) -> tuple[list[ParsedEntry], dict]:
        return CodexTranscriptParser.parse_entries(raw_entries, pending_tools)

    def exit_command(self) -> str:
        return "/quit"  # codex's TUI quit (verified: "/quit exit Codex")

    def is_working(self, pane_text: str) -> bool:
        # Codex has no ─ chrome separator for is_claude_working to anchor on;
        # its status line carries a distinctive "(Ns • esc to interrupt)" counter.
        return is_codex_working(pane_text)

    def has_queued_input(self, pane_text: str) -> bool:
        # Codex's "Messages to be submitted after next tool call" hint (its
        # analog of Claude's "Press up to edit queued messages").
        return has_codex_queued_messages(pane_text)

    def unsent_in_composer(self, pane_text: str, sent: str) -> bool:
        # Codex draws no ─ chrome around its composer; not verified.
        return False

    # latest_context_tokens: Codex's usage block (event_msg/token_count) uses a
    # per-model window (e.g. 258k for gpt-5.5), not the fixed 1M the alert
    # thresholds assume — so context alerts are skipped for now (base returns
    # None). Wire per-model windows before enabling.

    def latest_usage(self, raw_entries: list[dict]) -> AgentUsage | None:
        # event_msg/token_count carries the context fill, the model's window
        # and the account's rate limits; turn_context names the model.
        found = False
        tokens = window = None
        model: str | None = None
        limits: tuple[UsageLimit, ...] = ()
        for raw in raw_entries:
            payload = raw.get("payload")
            if not isinstance(payload, dict):
                continue
            if raw.get("type") == "turn_context" and isinstance(
                payload.get("model"), str
            ):
                model, found = payload["model"], True
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if isinstance(info, dict):
                last = info.get("last_token_usage")
                if isinstance(last, dict) and isinstance(last.get("total_tokens"), int):
                    tokens, found = last["total_tokens"], True
                if isinstance(info.get("model_context_window"), int):
                    window = info["model_context_window"]
            rl = payload.get("rate_limits")
            if isinstance(rl, dict):
                got = []
                for name in ("primary", "secondary"):
                    b = rl.get(name)
                    if isinstance(b, dict) and isinstance(
                        b.get("used_percent"), int | float
                    ):
                        mins = b.get("window_minutes")
                        mins = mins if isinstance(mins, int) else None
                        resets = b.get("resets_at")
                        got.append(
                            UsageLimit(
                                id=limit_id(mins, name),
                                pct_used=float(b["used_percent"]),
                                resets_at=resets if isinstance(resets, int) else None,
                                window_minutes=mins,
                            )
                        )
                if got:
                    limits, found = tuple(got), True
        if not found:
            return None
        return AgentUsage(
            context_tokens=tokens, context_window=window, model=model, limits=limits
        )

    def _rollout_cwd(self, rollout: Path) -> str | None:
        """session_meta.cwd of a codex rollout (cached; the header is immutable)."""
        key = str(rollout)
        if key in self._meta_cwd:
            return self._meta_cwd[key]
        cwd: str | None = None
        try:
            with open(rollout, "r", encoding="utf-8") as fh:
                first = fh.readline()
            data = json.loads(first)
            payload = data.get("payload")
            src = payload if isinstance(payload, dict) else data
            cw = src.get("cwd")
            cwd = cw if isinstance(cw, str) else None
        except (OSError, json.JSONDecodeError, ValueError):
            cwd = None
        self._meta_cwd[key] = cwd
        return cwd

    def _rollout_files_newest_first(self) -> list[Path]:
        """All rollout files under the sessions root, newest first ([] on error)."""
        root = config.codex_sessions_path
        if not root.exists():
            return []
        try:
            return sorted(
                root.rglob("rollout-*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return []

    def _sticky_rollout(self, cwd: str) -> Path | None:
        """Last rollout resolved for ``cwd``, if it still exists and matches."""
        cached = self._last_rollout.get(cwd)
        if cached and cached.exists() and _same_dir(self._rollout_cwd(cached), cwd):
            return cached
        return None

    def _resolve_rollout(
        self, cwd: str, files: list[Path] | None = None
    ) -> Path | None:
        """Newest codex rollout whose session_meta.cwd matches ``cwd``.

        The live session is the most-recently-written file, so a restart or
        /clear that begins a fresh rollout is picked up automatically next
        tick. ``files`` lets iter_transcripts share ONE directory scan across
        all codex windows. When the capped scan misses (busy hosts push a
        quiet window's rollout past the prefix), the sticky last-resolved path
        keeps the topic tracking instead of silently going dark.
        """
        if files is None:
            files = self._rollout_files_newest_first()
        for f in files[:_CODEX_SCAN_CAP]:
            if _same_dir(self._rollout_cwd(f), cwd):
                self._last_rollout[cwd] = f
                return f
        return self._sticky_rollout(cwd)

    async def list_sessions(self, session_manager: Any, cwd: str) -> list[AgentSession]:
        """Codex rollouts whose session_meta.cwd matches ``cwd`` (newest first).

        Scans the newest ``_CODEX_LIST_SCAN_CAP`` rollouts (cheap cached
        first-line cwd read), then fully reads only the ≤10 that match to build
        summary + count — the same AgentSession the Claude picker shows.
        """
        files = self._rollout_files_newest_first()
        sessions: list[AgentSession] = []
        for f in files[:_CODEX_LIST_SCAN_CAP]:
            if len(sessions) >= _CODEX_LIST_MAX:
                break
            if not _same_dir(self._rollout_cwd(f), cwd):
                continue
            match = _CODEX_ROLLOUT_SID_RE.search(f.name)
            if not match or not is_valid_session_id(match.group(1)):
                continue
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    entries = [json.loads(ln) for ln in fh if ln.strip()]
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            summary, count = CodexTranscriptParser.summarize(entries)
            if count == 0:
                continue  # header-only rollout (no real turn yet) — skip
            sessions.append(
                AgentSession(
                    session_id=match.group(1),
                    summary=summary,
                    message_count=count,
                    file_path=str(f),
                )
            )
        return sessions

    async def iter_transcripts(
        self, session_manager: Any, monitor: Any, active_session_ids: set[str]
    ) -> list[TranscriptRef]:
        codex_windows = [
            (binding, ws)
            for binding, ws in list(session_manager.window_states.items())
            if getattr(ws, "runtime", CLAUDE_RUNTIME) == self.name
        ]
        if not codex_windows:
            return []
        # ONE directory scan shared by every codex window this tick — the
        # rglob+stat sort is the expensive part and is identical per window.
        files = self._rollout_files_newest_first()
        refs: list[TranscriptRef] = []
        for binding, ws in codex_windows:
            cwd = ws.cwd
            # cwd is normally set at bind time; recover it from the live tmux
            # window for codex windows created before that (or if it was lost).
            if not cwd:
                from .tmux_manager import tmux_manager

                win = await tmux_manager.find_window_by_id(binding)
                if win and win.cwd:
                    cwd = win.cwd
                    ws.cwd = cwd
                    session_manager.save_state()
                else:
                    continue
            rollout = self._resolve_rollout(cwd, files)
            if rollout is None:
                continue
            match = _CODEX_ROLLOUT_SID_RE.search(rollout.name)
            sid = match.group(1) if match else None
            if not sid:
                continue
            # Mirror the rollout's session id onto the window so routing works.
            if ws.session_id != sid:
                ws.session_id = sid
                session_manager.save_state()
            refs.append((sid, rollout))
        return refs

    async def history_transcript(
        self, session_manager: Any, window_id: str
    ) -> Path | None:
        # History reads the same rollout the monitor tracks — resolved by cwd,
        # not via the Claude projects tree (which never holds codex sessions).
        ws = session_manager.get_window_state(window_id)
        if not ws.cwd:
            return None
        return self._resolve_rollout(ws.cwd)


class GrokRuntime(AgentRuntime):
    """xAI Grok CLI ("Grok Build", the `grok` binary).

    Launch: bare ``grok`` (no ``--name`` flag; resume = ``grok --resume
    <uuid>``; exit = ``/quit``). An authenticated launch opens grok's welcome
    screen, whose composer is LIVE — typing + Enter submits a prompt and
    starts the session (verified on 0.2.111), so the pending topic message CAN
    be auto-forwarded (unlike codex's sign-in menu). An unauthenticated launch
    shows the browser-approval screen, surfaced via the GrokLogin UI pattern
    (photo + one-time code + OSC8-recovered accounts.x.ai URL).

    Transcript: grok has no kikubot session_map (its Claude-compat hook scan DOES
    run ``kikubot hook``, but grok's stdin payload is camelCase so the hook
    no-ops by design) — a window's session is resolved by cwd:
    ``~/.grok/active_sessions.json`` (live pid registry grok maintains) names
    the live session for the cwd; fallback is the newest session dir in the
    cwd's group (``~/.grok/sessions/<url-encoded-cwd>/``, matched by DECODING
    group names, so the exact encoding never matters). The monitored file is
    ``updates.jsonl`` — the only per-session log that survives ``/compact``
    without a rewrite (chat_history.jsonl is truncated in place, which would
    break byte-offset reads). Like codex, two live grok windows on one cwd
    would cross-talk — the same-cwd guard applies via ``uses_session_map``.

    /diff: none — grok renders edit diffs with colour-only line numbering (no
    ±  gutter), which diff_view's shared gutter check can't anchor on; wiring
    it needs a per-runtime gutter seam first.
    """

    name = GROK_RUNTIME
    display_name = "Grok"
    picker_icon = "⚫"
    # No usable SessionStart session_map (see class docstring) — windows are
    # tracked by cwd, so bootstrap persists cwd and skips the hook wait/alarm.
    uses_session_map = False
    # Verified live: the welcome screen's composer accepts blind-typed text
    # ~1.5 s after launch and Enter submits it as the first prompt. The
    # settle delay covers the boot window explicitly (see base class).
    auto_forward_first_message = True
    first_message_settle_sec = 1.5
    # Esc cancels a running turn immediately (draft preserved). No trailing
    # Ctrl-C: grok's Ctrl-C clears the draft / escalates toward quit, and an
    # idle DOUBLE-Esc opens the rewind picker — one Escape per /esc is safe.
    interrupt_keys = ("Escape",)
    # Probed live on 0.2.111: Shift+Tab cycles Normal → Plan → Always-approve
    # ("mode"), /effort, /compact, /clear (alias of /new), /model, /context,
    # Ctrl+B sends the running command to the background. EXCLUDED: "rewind".
    panel_actions = frozenset(
        {"mode", "effort", "compact", "clear", "model", "context", "mcp", "background"}
    )
    panel_slash_commands = {
        **AgentRuntime.panel_slash_commands,
        "mcp": "/mcps",  # grok's MCP modal command (plural, unlike claude/codex)
    }
    # native_image_input stays False: grok's composer creates image chips only
    # from paste/drag, not from a typed path — the text marker is the way in
    # (grok reads the file itself via its read tool; no sandbox in the way).

    def __init__(self) -> None:
        # cwd -> last resolved session dir. Sticky fallback mirroring codex's:
        # active_sessions.json can momentarily miss the entry (written after
        # launch, cleared on exit) and the group scan can race a fresh dir —
        # the cached path keeps a quiet window tracking. Re-checked (exists)
        # before use.
        self._last_session_dir: dict[str, Path] = {}

    def cli_command(self) -> str:
        return config.grok_command

    def exit_command(self) -> str:
        return "/quit"  # grok's TUI quit (alias /exit; verified 0.2.111)

    def is_working(self, pane_text: str) -> bool:
        # Grok has no ─ chrome separator; its status row's "⇣<tokens> …
        # [stop]" right side is the busy signal.
        return is_grok_working(pane_text)

    def has_queued_input(self, pane_text: str) -> bool:
        # Grok's "Queued · Enter to send now" hint (analog of Claude's
        # "Press up to edit queued messages").
        return has_grok_queued_messages(pane_text)

    def unsent_in_composer(self, pane_text: str, sent: str) -> bool:
        # Grok's box (╭─╮ / │ ❯ … │) is not Claude's; not verified live.
        return False

    def launch_command(
        self, window_name: str, resume_session_id: str | None = None
    ) -> str:
        grok = config.grok_command
        if resume_session_id and is_valid_session_id(resume_session_id):
            return f"{grok} --resume {resume_session_id}"
        elif resume_session_id:
            logger.warning(
                "Ignoring malformed resume session id for grok window %s; "
                "starting fresh",
                window_name,
            )
        return grok

    def parse_entries(
        self, raw_entries: list[dict], pending_tools: dict
    ) -> tuple[list[ParsedEntry], dict]:
        return GrokTranscriptParser.parse_entries(raw_entries, pending_tools)

    # latest_context_tokens: grok's turn_completed carries usage (inputTokens/
    # totalTokens), but the context window is per-model (grok-4.5 shows 500K),
    # not the fixed 1M the alert thresholds assume — skipped like codex's.

    def _read_active_sessions(self) -> list[dict]:
        """Entries of grok's live-process registry ([] on any error).

        ``active_sessions.json`` sits next to the sessions root (normally
        ``~/.grok/``): grok appends {session_id, pid, cwd, opened_at} on
        launch and removes the entry on clean exit — the authoritative "which
        session is live in this cwd" source.
        """
        path = config.grok_sessions_path.parent / "active_sessions.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        live: list[dict] = []
        for e in data:
            if not isinstance(e, dict):
                continue
            # Drop entries whose pid is dead: an uncleanly-killed grok (OOM,
            # SIGKILL) leaves its registry row behind, and a newer-opened_at
            # dead row would otherwise outrank the LIVE session for the same
            # cwd — pinning the topic to a transcript nothing writes to.
            pid = e.get("pid")
            if isinstance(pid, int):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                except (PermissionError, OSError):
                    pass  # pid exists but isn't ours / unknown — keep the row
            live.append(e)
        return live

    def _group_dir_for_cwd(self, cwd: str) -> Path | None:
        """The sessions group directory whose (decoded) name is ``cwd``.

        Group names are the URL-encoded cwd; names over 255 bytes become a
        slug+hash with the original path in a ``.cwd`` file inside. Matching
        by DECODING (unquote / .cwd read-back) sidesteps ever reproducing
        grok's exact encoder.
        """
        root = config.grok_sessions_path
        if not root.is_dir():
            return None
        try:
            candidates = list(root.iterdir())
        except OSError:
            return None
        for d in candidates:
            if not d.is_dir():
                continue
            cwd_file = d / ".cwd"
            if cwd_file.is_file():
                try:
                    decoded = cwd_file.read_text().strip()
                except OSError:
                    continue
            else:
                decoded = urllib.parse.unquote(d.name)
            if _same_dir(decoded, cwd):
                return d
        return None

    def _resolve_session_dir(self, cwd: str) -> Path | None:
        """The session directory of the live grok session for ``cwd``.

        active_sessions.json names the live session (newest ``opened_at`` on a
        cwd collision); fallback is the group's newest session dir by
        updates.jsonl mtime (covers a just-exited grok whose registry entry is
        gone, e.g. for history views), then the sticky last-resolved dir.
        """
        group = self._group_dir_for_cwd(cwd)
        live = [
            e
            for e in self._read_active_sessions()
            if _same_dir(e.get("cwd"), cwd) and isinstance(e.get("session_id"), str)
        ]
        if live and group is not None:
            newest = max(live, key=lambda e: str(e.get("opened_at") or ""))
            candidate = group / newest["session_id"]
            if (candidate / "updates.jsonl").is_file():
                self._last_session_dir[cwd] = candidate
                return candidate
        if group is not None:
            best: Path | None = None
            best_mtime = -1.0
            try:
                subdirs = list(group.iterdir())
            except OSError:
                subdirs = []
            for d in subdirs:
                try:
                    mtime = (d / "updates.jsonl").stat().st_mtime
                except OSError:
                    continue
                if mtime > best_mtime:
                    best, best_mtime = d, mtime
            if best is not None:
                self._last_session_dir[cwd] = best
                return best
        cached = self._last_session_dir.get(cwd)
        if cached is not None and (cached / "updates.jsonl").is_file():
            return cached
        return None

    async def iter_transcripts(
        self, session_manager: Any, monitor: Any, active_session_ids: set[str]
    ) -> list[TranscriptRef]:
        grok_windows = [
            (binding, ws)
            for binding, ws in list(session_manager.window_states.items())
            if getattr(ws, "runtime", CLAUDE_RUNTIME) == self.name
        ]
        if not grok_windows:
            return []
        refs: list[TranscriptRef] = []
        for binding, ws in grok_windows:
            cwd = ws.cwd
            # cwd is normally set at bind time; recover it from the live tmux
            # window if it was lost (mirrors CodexRuntime.iter_transcripts).
            if not cwd:
                from .tmux_manager import tmux_manager

                win = await tmux_manager.find_window_by_id(binding)
                if win and win.cwd:
                    cwd = win.cwd
                    ws.cwd = cwd
                    session_manager.save_state()
                else:
                    continue
            session_dir = self._resolve_session_dir(cwd)
            if session_dir is None:
                continue
            sid = session_dir.name
            if not is_valid_session_id(sid):
                continue
            transcript = session_dir / "updates.jsonl"
            # Mirror the session id onto the window so routing works.
            if ws.session_id != sid:
                ws.session_id = sid
                session_manager.save_state()
            refs.append((sid, transcript))
        return refs

    def _summary_title(self, session_dir: Path) -> str:
        """The session's own generated title from summary.json ("" if none)."""
        try:
            data = json.loads((session_dir / "summary.json").read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return ""
        if not isinstance(data, dict):
            return ""
        for key in ("generated_title", "session_summary"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:50]
        return ""

    async def list_sessions(self, session_manager: Any, cwd: str) -> list[AgentSession]:
        """Grok sessions recorded for ``cwd`` (newest first, capped).

        The group directory IS the per-cwd index, so unlike codex there's no
        global scan: enumerate its session dirs, newest updates.jsonl first,
        and full-read only the top ``_GROK_LIST_MAX`` for the row summary
        (grok's own generated title when present, else the first user line).
        """
        group = self._group_dir_for_cwd(cwd)
        if group is None:
            return []
        rows: list[tuple[float, Path]] = []
        try:
            subdirs = list(group.iterdir())
        except OSError:
            return []
        for d in subdirs:
            if not is_valid_session_id(d.name):
                continue
            try:
                mtime = (d / "updates.jsonl").stat().st_mtime
            except OSError:
                continue
            rows.append((mtime, d))
        rows.sort(key=lambda r: r[0], reverse=True)
        sessions: list[AgentSession] = []
        for _, d in rows[:_GROK_LIST_MAX]:
            try:
                with open(d / "updates.jsonl", "r", encoding="utf-8") as fh:
                    entries = [json.loads(ln) for ln in fh if ln.strip()]
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            summary, count = GrokTranscriptParser.summarize(entries)
            if count == 0:
                continue  # header-only session (no real turn yet) — skip
            sessions.append(
                AgentSession(
                    session_id=d.name,
                    summary=self._summary_title(d) or summary,
                    message_count=count,
                    file_path=str(d / "updates.jsonl"),
                )
            )
        return sessions

    async def history_transcript(
        self, session_manager: Any, window_id: str
    ) -> Path | None:
        # History reads the same updates.jsonl the monitor tracks — resolved
        # by cwd, not via the Claude projects tree.
        ws = session_manager.get_window_state(window_id)
        if not ws.cwd:
            return None
        session_dir = self._resolve_session_dir(ws.cwd)
        return (session_dir / "updates.jsonl") if session_dir else None


class GlmRuntime(ClaudeRuntime):
    """GLM (z.ai Coding Plan) — Claude Code itself, served by z.ai's endpoint.

    z.ai ships no terminal CLI of its own: its supported path is Claude Code
    with ``ANTHROPIC_BASE_URL`` pointed at its Anthropic-compatible API and the
    Claude model aliases mapped onto GLM models. So the pane, the transcripts,
    the SessionStart hook and every TUI anchor are Claude's; the only
    divergence is the launch line, which adds ``--settings <file>`` — that
    file's ``env`` block (endpoint, key, model mapping) overlays THIS process
    only, while ``~/.claude/settings.json`` — every other agent — stays on
    Anthropic. No settings file ⇒ no picker tab.

    Shared with Claude, by construction: ``~/.claude/projects`` (so both tabs
    list the same resumable sessions — resuming one under the other just
    changes who answers) and the ``claude`` binary (so its version canary is
    Claude's).
    """

    name = GLM_RUNTIME
    display_name = "GLM"
    picker_icon = "🟢"
    # Its own --settings carries the z.ai env; and Claude's account limits
    # would be the wrong limits for a z.ai agent anyway.
    statusline_tap = False
    # The aliases map onto z.ai models there; Claude's names would mislead.
    model_aliases = ()

    def is_available(self) -> bool:
        return super().is_available() and config.glm_settings_path.is_file()

    def _claude_invocation(self) -> str:
        path = shlex.quote(str(config.glm_settings_path))
        return f"{config.claude_command} --settings {path}"

    async def cli_version(self) -> str | None:
        # Same binary as Claude: its canary already watches it — a second
        # probe would only double every self-update warning.
        return None

    async def iter_transcripts(
        self, session_manager: Any, monitor: Any, active_session_ids: set[str]
    ) -> list[TranscriptRef]:
        # A GLM window registers through the same SessionStart hook, so its
        # session is already in Claude's pass (which keys on the session_map,
        # not on the window's runtime). Returning it here too would deliver
        # every reply twice.
        return []


CLAUDE = ClaudeRuntime()
CODEX = CodexRuntime()
GROK = GrokRuntime()
GLM = GlmRuntime()

RUNTIMES: dict[str, AgentRuntime] = {
    CLAUDE.name: CLAUDE,
    CODEX.name: CODEX,
    GROK.name: GROK,
    GLM.name: GLM,
}


def pickable_runtimes() -> list[AgentRuntime]:
    """Runtimes offered in the session picker's agent menu, in display order.

    Registered runtimes whose CLI is actually installed (``is_available``) —
    a fresh install without codex must never see a Codex tab whose "new
    session" types a missing command into the pane (dead window, no
    explanation). Registry insertion order (Claude, Codex, …); a new runtime
    added to ``RUNTIMES`` becomes a picker tab automatically. Falls back to
    Claude if nothing probes available (main.py already warns at boot), so the
    picker is never empty.
    """
    available = [rt for rt in RUNTIMES.values() if rt.is_available()]
    return available or [CLAUDE]


def default_runtime() -> AgentRuntime:
    """The runtime new windows default to (``KIKUBOT_DEFAULT_RUNTIME``).

    Resolves the configured name through the registry (unknown → Claude) and
    falls back to Claude when the configured runtime's CLI isn't installed —
    the knob must never produce windows that can't launch.
    """
    rt = get_runtime(config.default_runtime)
    return rt if rt.is_available() else CLAUDE


def monitored_runtimes() -> list[AgentRuntime]:
    """Runtimes the monitor iterates each tick.

    All registered runtimes: each ``iter_transcripts`` is cheap when that
    runtime has no bound windows (Claude's scan is already done regardless;
    Codex returns early when no codex windows exist / no sessions dir).
    """
    return list(RUNTIMES.values())


def is_valid_runtime(name: str | None) -> bool:
    """True if ``name`` is a known runtime identifier."""
    return bool(name) and name in RUNTIMES


def get_runtime(name: str | None) -> AgentRuntime:
    """Resolve a runtime name to its ``AgentRuntime``.

    Unknown/empty names fall back to Claude — legacy ``WindowState`` rows carry
    no runtime, and an unrecognised value must degrade to the default rather
    than crash a launch/monitor path.
    """
    if name and name in RUNTIMES:
        return RUNTIMES[name]
    return CLAUDE
