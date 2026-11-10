"""Agent status — one topic's agent as a readable card and as JSON (format v1).

Answers the inline query ``status`` (handlers/inline_status.py); the format is
specified in docs/agent-status-format.md, which clients code against. Built
only from caches, so an answer never waits on tmux or a transcript read:

- the pane the 1 s status poll last captured (``note_pane``) → working / idle /
  waiting for an answer;
- what the monitor last read from the session's transcript (``note_usage``,
  via ``AgentRuntime.latest_usage``) → context fill, the model that answered,
  Codex's usage limits;
- the snapshot ``kikubot statusline`` wrote for the window (statusline.py,
  opt-in ``KIKUBOT_USAGE_LIMITS``) → Claude's selected model, exact context
  window, account usage limits, effort, session stats.

Extending it: anything that fits «label: value» is an ``ITEM_PROVIDERS``
function — it reaches every client as a row of ``items`` with no client
release. A new top-level field is for data a client draws in its own way.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .config import config
from .i18n import tr
from .runtimes import AgentUsage, UsageLimit, get_runtime, limit_id

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
MAX_JSON_CHARS = 4000  # Telegram caps an inline result's message_text at 4096

# A pane note older than this says nothing about the agent's state any more
# (the poll refreshes bound windows every second).
PANE_FRESH_SEC = 15.0

# Claude Code's names for its limit windows → their length. A per-model
# window carries the model after the base («seven_day_opus»).
_CLAUDE_LIMIT_MINUTES = {"five_hour": 300, "seven_day": 10080}


def _claude_limit(name: str) -> tuple[str, int | None]:
    """(id, minutes) of a Claude limit: «seven_day_opus» → ("7d_opus", 10080)."""
    for base, mins in _CLAUDE_LIMIT_MINUTES.items():
        if name == base:
            return limit_id(mins, name), mins
        if name.startswith(base + "_"):
            return f"{limit_id(mins, base)}_{name[len(base) + 1 :]}", mins
    return name, None


# Sections a client shows first (in this order, those present); the rest go
# behind a «more» step. What changes often and matters now.
SUMMARY_IDS = ("model", "effort", "mode", "context")

# Icon names of the permission modes (docs/agent-status-format.md lists them).
MODE_ICONS = {
    "manual": "hand",
    "accept_edits": "code",
    "plan": "plan",
    "auto": "bolt",
    "bypass": "warning",
}


def _mode_ids(offered: tuple[str, ...], current: str | None) -> list[str]:
    """The runtime's modes, plus the current one if it isn't among them
    (bypass exists only when the agent was launched with it)."""
    ids = list(offered)
    if current and current not in ids:
        ids.append(current)
    return ids


# The panel actions offered as quick commands, in this order. /clear is left
# out on purpose: one tap in a sheet must not wipe the session.
_QUICK_ACTIONS = ("compact", "context", "model")

_panes: dict[str, tuple[float, str | None]] = {}
_usage: dict[str, AgentUsage] = {}


def note_pane(binding: str, pane_text: str | None) -> None:
    """Remember the pane the status poll just captured (None = unreadable)."""
    _panes[binding] = (time.monotonic(), pane_text)


def note_usage(session_id: str, usage: AgentUsage) -> None:
    """Merge a transcript batch's usage into the session's (newer fields win)."""
    old = _usage.get(session_id)
    if old is None:
        _usage[session_id] = usage
        return
    _usage[session_id] = AgentUsage(
        context_tokens=usage.context_tokens
        if usage.context_tokens is not None
        else old.context_tokens,
        context_window=usage.context_window or old.context_window,
        model=usage.model or old.model,
        limits=usage.limits or old.limits,
    )


def forget(binding: str) -> None:
    """Drop the pane note of a binding that went away."""
    _panes.pop(binding, None)


@dataclass(frozen=True)
class Item:
    """One «label: value» row; clients show unknown ids as they are."""

    id: str
    label: str
    value: str


@dataclass
class AgentStatus:
    """Everything known about one agent, ready for the card and the JSON."""

    name: str
    runtime: str
    ts: int
    state: str | None = None
    model: str | None = None
    model_running: str | None = None
    context_pct: int | None = None
    context_used: int | None = None
    context_window: int | None = None
    limits: list[UsageLimit] = field(default_factory=list)
    mode: str | None = None  # permission mode id, when the runtime has modes
    effort: str | None = None
    model_id: str | None = None  # the CLI's id of the selected model
    branch: str | None = None
    # alias → readable name with version, from models seen on this machine
    alias_names: dict[str, str] = field(default_factory=dict)
    items: list[Item] = field(default_factory=list)
    commands: list[tuple[str, str]] = field(default_factory=list)

    def sections(self) -> list[dict]:
        """The server-driven sheet: ordered sections a client renders by type."""
        rt = get_runtime(self.runtime)
        out: list[dict] = []
        if self.model:
            out.append(_model_section(self, rt.model_aliases))
        if self.effort and self.effort in rt.effort_levels:
            out.append(
                {
                    "type": "choice",
                    "id": "effort",
                    "title": tr("astatus.item.effort"),
                    "subtitle": tr("astatus.effort_scope"),
                    "selected": self.effort,
                    "command": "/effort {id}",
                    "options": [
                        {"id": lvl, "label": tr(f"astatus.effort.{lvl}")}
                        for lvl in rt.effort_levels
                    ],
                }
            )
        if self.context_pct is not None or self.context_used:
            sec: dict = {
                "type": "meter",
                "id": "context",
                "title": tr("astatus.sec.context"),
            }
            if self.context_pct is not None:
                sec["pct"] = self.context_pct
            caption = _context_caption(self)
            if caption:
                sec["caption"] = caption
            out.append(sec)
        if self.limits:
            out.append(
                {
                    "type": "meters",
                    "id": "limits",
                    "title": tr("astatus.sec.limits"),
                    "items": [_limit_meter(lim) for lim in self.limits],
                }
            )
        if self.mode:
            out.append(
                {
                    "type": "choice",
                    "id": "mode",
                    "title": tr("astatus.item.mode"),
                    "selected": self.mode,
                    "command": "/mode {id}",
                    "options": [
                        {
                            "id": m,
                            "label": tr(f"astatus.mode.{m}"),
                            "subtitle": tr(f"astatus.mode_sub.{m}"),
                            "icon": MODE_ICONS.get(m, ""),
                        }
                        for m in _mode_ids(rt.permission_modes, self.mode)
                    ],
                }
            )
        if self.items:
            out.append(
                {
                    "type": "rows",
                    "id": "session",
                    "title": tr("astatus.sec.session"),
                    "items": [
                        {"id": i.id, "label": i.label, "value": i.value}
                        for i in self.items
                    ],
                }
            )
        if self.commands:
            out.append(
                {
                    "type": "actions",
                    "id": "commands",
                    "title": tr("astatus.sec.commands"),
                    "items": [{"label": lbl, "cmd": c} for c, lbl in self.commands],
                }
            )
        return out

    def to_json(self) -> str:
        out: dict = {"v": FORMAT_VERSION, "runtime": self.runtime, "ts": self.ts}
        if self.state:
            out["state"] = self.state
        if self.model:
            out["model"] = self.model
        if self.model_running:
            out["model_running"] = self.model_running
        if self.branch:
            out["branch"] = self.branch
        ctx = {
            k: v
            for k, v in (
                ("pct", self.context_pct),
                ("used", self.context_used),
                ("window", self.context_window),
            )
            if v is not None
        }
        if ctx:
            out["context"] = ctx
        if self.limits:
            out["limits"] = [_limit_json(lim) for lim in self.limits]
        sections = self.sections()
        while True:
            if sections:
                out["sections"] = sections
                ids = {s["id"] for s in sections}
                out["summary"] = [i for i in SUMMARY_IDS if i in ids]
            text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
            # message_text over Telegram's cap fails the WHOLE inline answer:
            # drop sections from the end until it fits.
            if len(text) <= MAX_JSON_CHARS or not sections:
                return text
            sections = sections[:-1]
            out.pop("sections", None)
            out.pop("summary", None)

    def summary(self) -> str:
        """One line for an inline result's description."""
        parts = [tr(f"astatus.state.{self.state}") if self.state else ""]
        if self.context_pct is not None:
            parts.append(tr("astatus.summary_ctx", pct=self.context_pct))
        for lim in self.limits[:2]:
            parts.append(f"{_limit_label(lim.id)} {round(lim.pct_used)}%")
        return " · ".join(p for p in parts if p)

    def card(self, now: float | None = None) -> str:
        """The readable card (plain text, no markup)."""
        now = time.time() if now is None else now
        rt = get_runtime(self.runtime)
        lines = [f"👾 {self.name} · {rt.display_name}"]
        if self.state:
            lines.append(tr(f"astatus.state.{self.state}"))
        if self.model:
            lines.append(tr("astatus.model", model=self.model))
        if self.model_running and self.model_running != self.model:
            lines.append(tr("astatus.model_running", model=self.model_running))
        ctx = _context_line(self)
        if ctx:
            lines.append(ctx)
        for lim in self.limits:
            pct = round(lim.pct_used)
            line = tr(
                "astatus.limit",
                label=_limit_label(lim.id),
                used=pct,
                left=max(0, 100 - pct),
            )
            if lim.resets_at:
                line += tr("astatus.resets", when=_in(lim.resets_at - now))
            lines.append(line)
        if self.effort:
            lines.append(f"{tr('astatus.item.effort')}: {self.effort}")
        if self.mode:
            lines.append(
                f"{tr('astatus.item.mode')}: {tr(f'astatus.mode.{self.mode}')}"
            )
        lines += [f"{i.label}: {i.value}" for i in self.items]
        return "\n".join(lines)


def _model_section(st: AgentStatus, aliases: tuple[tuple[str, str], ...]) -> dict:
    """The model picker: the CLI's aliases, or just the current model + /model."""
    current = (st.model_id or st.model or "").lower()
    selected = next((a for a, _ in aliases if a in current), None)
    sec: dict = {"type": "choice", "id": "model", "title": tr("astatus.sec.model")}
    if selected is None:
        # No alias list (or an unknown model): show it; a tap opens the CLI's
        # own picker.
        sec.update(
            selected="current",
            command="/model",
            options=[{"id": "current", "label": st.model}],
        )
        return sec
    sec.update(
        subtitle=tr("astatus.model_scope"),
        selected=selected,
        command="/model {id}",
        options=[
            {
                "id": a,
                "label": st.model if a == selected else st.alias_names.get(a, label),
            }
            for a, label in aliases
        ],
    )
    return sec


def _limit_meter(lim: UsageLimit) -> dict:
    pct = round(lim.pct_used)
    item: dict = {
        "label": _limit_label(lim.id),
        "pct": pct,
        "caption": tr("astatus.left", left=max(0, 100 - pct)),
    }
    if lim.resets_at:
        item["resets_at"] = lim.resets_at
    return item


def _context_caption(st: AgentStatus) -> str | None:
    if st.context_used and st.context_window:
        return tr("astatus.of", used=_k(st.context_used), window=_k(st.context_window))
    if st.context_used:
        return tr("astatus.tokens", used=_k(st.context_used))
    return None


def _limit_json(lim: UsageLimit) -> dict:
    out: dict = {
        "id": lim.id,
        "label": _limit_label(lim.id),
        "pct_used": round(lim.pct_used),
    }
    if lim.resets_at:
        out["resets_at"] = lim.resets_at
    if lim.window_minutes:
        out["window_minutes"] = lim.window_minutes
    return out


def _limit_label(limit: str) -> str:
    """«5 h», «7 days», «7 days · Opus»; an unknown id as it is."""
    base, _, model = limit.partition("_")
    label = tr(f"astatus.limit.{base}")
    if label.startswith("astatus."):
        return limit
    return f"{label} · {model.replace('_', ' ').title()}" if model else label


def _base_model(model_id: str) -> str:
    """A model id without the context-size tag («claude-opus-5-5[1m]»)."""
    return model_id.split("[", 1)[0].lower()


def _k(tokens: int) -> str:
    return f"{tokens / 1000:.0f}k" if tokens < 1_000_000 else f"{tokens / 1e6:.1f}M"


def _context_line(st: AgentStatus) -> str | None:
    if st.context_pct is not None and st.context_used and st.context_window:
        return tr(
            "astatus.context_full",
            pct=st.context_pct,
            used=_k(st.context_used),
            window=_k(st.context_window),
        )
    if st.context_pct is not None:
        return tr("astatus.context_pct", pct=st.context_pct)
    if st.context_used:
        return tr("astatus.context_used", used=_k(st.context_used))
    return None


def _in(seconds: float) -> str:
    """«2 h 15 min» / «40 min» / «3 d 4 h» until a moment."""
    mins = max(0, int(seconds // 60))
    days, rem = divmod(mins, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return tr("astatus.dur_dh", d=days, h=hours)
    if hours:
        return tr("astatus.dur_hm", h=hours, m=mins)
    return tr("astatus.dur_m", m=mins)


# --- statusline snapshots ---------------------------------------------------


def _read_snapshot(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _session_map_sid(window_id: str) -> str | None:
    """The session id the SessionStart hook reported for a window."""
    try:
        data = json.loads(config.session_map_file.read_text(encoding="utf-8"))
        entry = data.get(f"{config.tmux_session_name}:{window_id}") or {}
    except (OSError, ValueError, AttributeError):
        return None
    sid = entry.get("session_id") if isinstance(entry, dict) else None
    return sid if isinstance(sid, str) else None


def window_snapshot(window_id: str, tracked_sid: str = "") -> dict | None:
    """The statusline snapshot of a tmux window, if it is this session's.

    Window ids restart with the tmux server, so a file can be a previous
    window's: it counts only when its session is the window's — the hook's id
    (what a resumed session reports) or the tracked one.
    """
    if not window_id.startswith("@"):
        return None
    snap = _read_snapshot(config.agent_status_dir / f"{window_id}.json")
    if not snap:
        return None
    sid = snap.get("session_id")
    if not sid or sid not in {tracked_sid, _session_map_sid(window_id)}:
        return None
    return snap


def observed_models() -> dict[str, tuple[str, int | None]]:
    """Model id → (readable name, context window) from every window's report.

    What this machine's Claude actually ran, so the model picker can name
    each alias with its current version and an old window can learn its
    model's window size — without a hardcoded model table that goes stale.
    """
    seen: dict[str, tuple[str, int | None]] = {}
    try:
        paths = list(config.agent_status_dir.glob("@*.json"))
    except OSError:
        return seen
    for path in paths:
        snap = _read_snapshot(path) or {}
        model = snap.get("model") or {}
        mid = model.get("id")
        if not isinstance(mid, str):
            continue
        window = (snap.get("context_window") or {}).get("context_window_size")
        seen[_base_model(mid)] = (
            model.get("display_name") or mid,
            window if isinstance(window, int) else None,
        )
    return seen


def _alias_names(
    aliases: tuple[tuple[str, str], ...],
    ids: Iterable[str],
    pretty: Callable[[str], str],
) -> dict[str, str]:
    """alias → the newest version of it among ``ids``, readably named."""
    out: dict[str, tuple[str, str]] = {}
    for mid in ids:
        for alias, _ in aliases:
            if alias in mid and (alias not in out or mid > out[alias][0]):
                out[alias] = (mid, pretty(mid))
    return {a: name for a, (_, name) in out.items()}


def _snapshot_limits(snap: dict | None, now: float) -> list[UsageLimit]:
    """Claude's limits from a snapshot, minus windows that already reset."""
    if not snap:
        return []
    out = []
    for name, block in (snap.get("rate_limits") or {}).items():
        if not isinstance(block, dict) or "used_percentage" not in block:
            continue
        resets = block.get("resets_at")
        if isinstance(resets, int | float) and resets < now:
            continue  # rolled over: the old figure is no longer true
        lid, mins = _claude_limit(name)
        out.append(
            UsageLimit(
                id=lid,
                pct_used=float(block["used_percentage"]),
                resets_at=int(resets) if isinstance(resets, int | float) else None,
                window_minutes=mins,
            )
        )
    return out


def account_limits(now: float | None = None) -> list[UsageLimit]:
    """The account's Claude limits, merged over every tapped window's report.

    Limits belong to the account, not the session, so any window's figure
    counts (also for windows launched before the tap). Per limit the newest
    window wins (latest reset time); within one window usage only grows, so
    the highest report is the current one.
    """
    now = time.time() if now is None else now
    best: dict[str, UsageLimit] = {}
    try:
        paths = list(config.agent_status_dir.glob("@*.json"))
    except OSError:
        return []
    for path in paths:
        for lim in _snapshot_limits(_read_snapshot(path), now):
            old = best.get(lim.id)
            if old is None or (lim.resets_at or 0, lim.pct_used) > (
                old.resets_at or 0,
                old.pct_used,
            ):
                best[lim.id] = lim
    return sorted(best.values(), key=lambda lim: (lim.window_minutes or 0, lim.id))


def write_statusline_settings() -> Path | None:
    """Write the --settings file that makes Claude run our status-line tap.

    Rewritten on every launch, so a moved install heals itself. Keeps the
    user's own statusLine display options (padding); the command it chains to
    is resolved by the tap at run time. None if it can't be written.
    """
    import shlex

    from .hook import _claude_settings_file, _find_kikubot_path

    try:
        user = json.loads(_claude_settings_file().read_text(encoding="utf-8"))
        user_sl = user.get("statusLine") if isinstance(user, dict) else None
    except (OSError, ValueError):
        user_sl = None
    command = " ".join(
        shlex.quote(p)
        for p in (
            _find_kikubot_path(),
            "statusline",
            str(config.agent_status_dir),
            config.tmux_session_name,
        )
    )
    sl: dict = {"type": "command", "command": command}
    if isinstance(user_sl, dict) and isinstance(user_sl.get("padding"), int):
        sl["padding"] = user_sl["padding"]
    path = config.config_dir / "agent-settings.json"
    try:
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"statusLine": sl}, fh)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("Cannot write %s, launching without usage limits: %s", path, e)
        return None
    return path


# --- extensible rows --------------------------------------------------------


@dataclass(frozen=True)
class Sources:
    """What an item provider may read."""

    binding: str
    runtime: str
    snapshot: dict | None
    usage: AgentUsage | None
    pane: str | None


def _effort_items(src: Sources) -> list[Item]:
    snap = src.snapshot or {}
    out = []
    if isinstance(snap.get("thinking"), bool):
        out.append(
            Item(
                "thinking",
                tr("astatus.item.thinking"),
                tr("astatus.on" if snap["thinking"] else "astatus.off"),
            )
        )
    if snap.get("fast_mode") is True:
        out.append(Item("fast_mode", tr("astatus.item.fast_mode"), tr("astatus.on")))
    return out


def _session_items(src: Sources) -> list[Item]:
    cost = (src.snapshot or {}).get("cost") or {}
    out = []
    added, removed = cost.get("total_lines_added"), cost.get("total_lines_removed")
    if added or removed:
        out.append(
            Item("lines", tr("astatus.item.lines"), f"+{added or 0} −{removed or 0}")
        )
    ms = cost.get("total_duration_ms")
    if ms:
        out.append(Item("duration", tr("astatus.item.duration"), _in(ms / 1000)))
    usd = cost.get("total_cost_usd")
    if usd:
        out.append(Item("cost", tr("astatus.item.cost"), f"${usd:.2f}"))
    return out


def _pane_items(src: Sources) -> list[Item]:
    """Permission mode and background shells, off the pane's footer."""
    if not src.pane:
        return []
    rt = get_runtime(src.runtime)
    out = []
    shells = rt.background_shells(src.pane)
    if shells:
        out.append(Item("shells", tr("astatus.item.shells"), str(shells)))
    return out


def git_branch(cwd: str) -> str | None:
    """The checked-out branch of ``cwd`` (short sha if detached), by reading
    .git/HEAD — no git subprocess on the inline path."""
    git = Path(cwd) / ".git"
    try:
        if git.is_file():  # a worktree: «gitdir: <path>»
            ref = git.read_text(encoding="utf-8").strip()
            if not ref.startswith("gitdir:"):
                return None
            git = (Path(cwd) / ref.split(":", 1)[1].strip()).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/") :]
    return head[:8] or None


def _branch_items(src: Sources) -> list[Item]:
    from .session import session_manager

    ws = session_manager.window_states.get(src.binding)
    cwd = getattr(ws, "cwd", "") if ws else ""
    if not cwd or src.binding.startswith("docker:"):
        return []  # a container's /workspace is not a host path
    branch = git_branch(cwd)
    return [Item("branch", tr("astatus.item.branch"), branch)] if branch else []


def _queue_items(src: Sources) -> list[Item]:
    from .session import session_manager

    if src.pane and session_manager.agent_has_queued_input(src.binding, src.pane):
        return [Item("queued", tr("astatus.item.queued"), tr("astatus.yes"))]
    return []


ItemProvider = Callable[[Sources], list[Item]]

# Add a function here to show one more thing; order = display order.
ITEM_PROVIDERS: list[ItemProvider] = [
    _pane_items,
    _effort_items,
    _branch_items,
    _session_items,
    _queue_items,
]


# --- assembly ---------------------------------------------------------------


def _state(binding: str, runtime: str) -> tuple[str | None, str | None]:
    """(state, pane) from the poll's last capture."""
    from .session import session_manager
    from .terminal_parser import is_interactive_ui

    note = _panes.get(binding)
    if note is None or time.monotonic() - note[0] > PANE_FRESH_SEC:
        return None, None
    pane = note[1]
    if not pane:
        return "gone", None
    if is_interactive_ui(pane):
        return "waiting", pane
    if session_manager.is_agent_working(binding, pane):
        return "working", pane
    return "idle", pane


def quick_commands(runtime: str, reserved: Iterable[str]) -> list[tuple[str, str]]:
    """(slash, label) the client may send as a message to reach the agent.

    A slash the bot itself handles (``reserved`` — e.g. Codex's /status vs the
    bot's /status) would never reach the agent, so it's left out.
    """
    rt = get_runtime(runtime)
    taken = {r.lstrip("/").lower() for r in reserved}
    out = []
    for action in _QUICK_ACTIONS:
        if not rt.supports_panel_action(action):
            continue
        slash = rt.panel_slash(action)
        if not slash or slash.split()[0].lstrip("/").lower() in taken:
            continue
        out.append((slash, tr(f"astatus.cmd.{action}")))
    return out


def build_status(
    binding: str, reserved_commands: Iterable[str] = (), now: float | None = None
) -> AgentStatus:
    """Assemble the status of the agent behind ``binding`` from the caches."""
    from .session import session_manager

    now = time.time() if now is None else now
    runtime = session_manager.window_runtime(binding)
    rt = get_runtime(runtime)
    ws = session_manager.window_states.get(binding)
    usage = _usage.get(ws.session_id) if ws and ws.session_id else None
    tapped = rt.statusline_tap and config.usage_limits_enabled
    snap = window_snapshot(binding, ws.session_id if ws else "") if tapped else None
    state, pane = _state(binding, runtime)

    st = AgentStatus(
        name=session_manager.get_display_name(binding),
        runtime=runtime,
        ts=int(now),
        state=state,
    )
    snap_model = (snap or {}).get("model") or {}
    st.model = snap_model.get("display_name") or snap_model.get("id")
    st.model_id = snap_model.get("id") or (usage.model if usage else None)
    effort = (snap or {}).get("effort")
    if isinstance(effort, str):
        st.effort = effort
    elif rt.effort_levels:
        st.effort = rt.configured_effort(st.model_id)
    if usage and usage.model:
        if st.model is None:
            st.model = rt.pretty_model(usage.model)
        elif _base_model(usage.model) != _base_model(snap_model.get("id") or ""):
            st.model_running = rt.pretty_model(usage.model)

    observed = observed_models() if rt.model_aliases else {}
    if rt.model_aliases:
        ids = set(observed) | {u.model for u in _usage.values() if u.model}
        names = {mid: name for mid, (name, _) in observed.items()}
        st.alias_names = _alias_names(
            rt.model_aliases, ids, lambda m: names.get(m) or rt.pretty_model(m)
        )

    ctx = (snap or {}).get("context_window") or {}
    st.context_window = ctx.get("context_window_size") or (
        usage.context_window if usage else None
    )
    st.context_used = usage.context_tokens if usage else None
    pct = ctx.get("used_percentage")
    if isinstance(pct, int | float):
        # One source for both numbers: the transcript's last usage lags behind
        # the status line after a /compact.
        st.context_pct = round(pct)
        if st.context_window:
            st.context_used = round(pct * st.context_window / 100)
    elif st.context_used is not None:
        known = observed.get(_base_model(st.model_id or ""), ("", None))[1]
        st.context_window = (
            st.context_window or known or rt.infer_context_window(st.context_used)
        )
        if st.context_window:
            st.context_pct = round(100 * st.context_used / st.context_window)

    if tapped:
        # Limits belong to the account: the freshest report of any window
        # wins. A container logs in with its own account — no host figures.
        if binding.startswith("@"):
            st.limits = account_limits(now)
    elif usage:
        st.limits = [
            lim for lim in usage.limits if not lim.resets_at or lim.resets_at >= now
        ]

    if ws and getattr(ws, "cwd", "") and binding.startswith("@"):
        st.branch = git_branch(ws.cwd)

    if pane and rt.permission_modes and state != "waiting":
        st.mode = rt.permission_mode(pane)

    src = Sources(binding, runtime, snap, usage, pane)
    for provider in ITEM_PROVIDERS:
        try:
            st.items += provider(src)
        except Exception as e:  # noqa: BLE001 — one bad row must not sink the card
            logger.debug("status item provider %s failed: %s", provider.__name__, e)
    st.commands = quick_commands(runtime, reserved_commands)
    return st
