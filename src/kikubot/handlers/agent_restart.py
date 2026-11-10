"""Revive a topic's agent from scratch — same folder, same CLI, same session.

🔄 Restart means "bring back what was here", and that has to hold after the
window is already gone: an agent that crashed (or was reaped as dead 30 s
later) leaves the topic unbound, and the panel/`/restart` used to answer
«window gone» — the only way back was a message, which now opens the session
picker. So the restart paths call :func:`revive_topic_agent`, which rebuilds
the window from what the topic remembers:

  - folder   — ``thread_directory_memory`` (survives the window),
  - runtime  — ``thread_runtime_memory`` (a codex topic comes back as codex),
  - session  — the window's own ``session_id`` while its state still exists,
    else the newest session of that runtime in the folder. Both resolve to a
    `--resume`, so the conversation continues instead of starting over.

This is the ONE path that creates a window without a picker tap, and that is
the point: the user asked for this exact agent back (see binding-flows.md —
auto-bind itself never starts a session).

A **docker** binding needs its own half (:func:`revive_docker_agent` and the
offer keyboard around it): there is no tmux window to rebuild and no folder to
pick — the agent is a tmux session *inside* the container, which a
``docker restart`` wipes for every sibling (the entrypoint recreates only the
main one). The topic keeps its binding and its files, so recovery is just
"start that session again", either continuing a conversation or clean.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from ..agent_session import AgentSession
from ..i18n import tr
from ..runtimes import AgentRuntime, default_runtime, get_runtime
from ..session import session_manager
from ..terminal_parser import is_interactive_ui
from ..tmux_manager import TmuxReadError, tmux_manager
from ..utils import is_valid_session_id
from .callback_data import (
    CALLBACK_WID_MAX,
    CB_CMD_RESTART_STOP,
    CB_DVR_BACK,
    CB_DVR_FRESH,
    CB_DVR_LIST,
    CB_DVR_RESUME,
)
from .message_sender import safe_send

logger = logging.getLogger(__name__)


class ReviveError(Exception):
    """Revive failed. ``key`` is an i18n key, ``fmt`` its format arguments."""

    def __init__(self, key: str, **fmt: object) -> None:
        super().__init__(key)
        self.key = key
        self.fmt = fmt


EXIT_WAIT_SEC = 10.0
EXIT_FLUSH_WAIT_SEC = 5.0
_POLL_SEC = 0.3

# Windows a restart is working on, from the first check to the new CLI's
# input box. A second tap is refused instead of queueing a second restart
# behind the send lock, and /esc keeps its Ctrl-C off a half-typed relaunch.
_restarting: set[str] = set()


def is_restarting(window_id: str) -> bool:
    return window_id in _restarting


@dataclass(frozen=True)
class ExitResult:
    """How a restart's exit ended."""

    exited: bool
    # Set when the CLI asked about running work and the exit was cancelled:
    # what it listed. Nothing was stopped.
    background: tuple[str, ...] = ()
    # Why nothing was typed at all: "busy", "dialog" or "restarting".
    refused: str = ""


def restart_blocker(window_id: str, pane: str | None) -> str:
    """Why a tmux window can't be restarted right now ("" = it can).

    The exit command and its Enter are typed into the pane: in a running turn
    they'd become a prompt, and in a dialog Enter takes the preselected
    option (approve the tool, accept the plan).
    """
    if window_id in _restarting:
        return "restarting"
    return _pane_blocker(window_id, pane)


def _pane_blocker(window_id: str, pane: str | None) -> str:
    if pane is None:
        # A docker agent with no pane is down, which is what a restart fixes.
        # A tmux pane that can't be read could hide a turn or a dialog: what
        # we'd type is not to be guessed.
        return "" if session_manager._is_docker_binding(window_id) else "unreadable"
    if session_manager.is_agent_working(window_id, pane):
        return "busy"
    if is_interactive_ui(pane):
        return "dialog"
    return ""


async def restart_precheck(window_id: str) -> ExitResult | None:
    """Why the agent can't be restarted right now, or None if it can.

    Both transports. A docker restart kills the in-container session
    outright, with no exit question to read, so the footer's count of
    background shells is what it has to go on.
    """
    pane = await session_manager.capture_pane(window_id)
    blocker = restart_blocker(window_id, pane)
    if blocker:
        return ExitResult(False, refused=blocker)
    if pane and session_manager._is_docker_binding(window_id):
        rt = get_runtime(session_manager.window_runtime(window_id))
        shells = rt.background_shells(pane)
        if shells:
            return ExitResult(False, (tr("restart.background_shells", n=shells),))
    return None


async def restart_docker_agent(
    window_id: str,
    resume_id: str | None,
    wait_ready: Callable[[str], Awaitable[None]] | None = None,
) -> bool:
    """Kill a docker binding's in-container session and start it again.

    Held under the send lock up to the new CLI's input box (``wait_ready``),
    and marked as restarting, so the dead-sibling poll doesn't take the
    gap between kill and start for a death and revive the old session over
    the new one. With no session to continue (🆕, or one emptied by /clear)
    the new one gets a kikubot-pinned id, as a sibling needs to stay
    trackable. True iff it started.
    """
    from ..docker_driver import docker_driver

    target = session_manager.resolve_docker_target(window_id)
    if target is None or window_id in _restarting:
        return False
    _restarting.add(window_id)
    try:
        async with session_manager.send_lock(window_id):
            await docker_driver.kill_session(
                target.agent.container, session=target.tmux_session
            )
            await asyncio.sleep(1)
            started = await session_manager.start_docker_agent(
                window_id,
                resume_session_id=resume_id or None,
                new_session_id=None if resume_id else str(uuid.uuid4()),
                cwd=session_manager.get_window_state(window_id).cwd or "/workspace",
            )
            if started and wait_ready is not None:
                await wait_ready(window_id)
            return started
    finally:
        _restarting.discard(window_id)


async def _press(window_id: str, key: str) -> None:
    await tmux_manager.send_keys(window_id, key, enter=False, literal=False)


async def _agent_state(window_id: str) -> tuple[bool | None, str]:
    """(agent still running, pane); running is None when tmux can't be read."""
    try:
        w = await tmux_manager.find_window_by_id(window_id)
    except TmuxReadError:
        return None, ""
    if w is None:
        return None, ""
    running = await tmux_manager.is_agent_running(w)
    return running, (await tmux_manager.capture_pane(window_id) or "")


async def exit_for_relaunch(
    window_id: str, runtime: AgentRuntime, *, stop_background: bool = False
) -> ExitResult:
    """Quit a tmux window's agent CLI back to its shell, for a relaunch.

    The caller holds ``send_lock``. Nothing on the way is confirmed blind,
    because Enter takes whatever the CLI preselected:

    - Claude Code asks before quitting with background work running (shells,
      scheduled tasks) and preselects «Exit and stop tasks». Esc cancels the
      exit and the result lists that work, unless ``stop_background``: the
      user already saw the list and chose to stop it.
    - A notice screen (Claude Code's feedback drafts) is closed with Esc,
      and the exit goes on.
    - The exit command's own Enter can be swallowed by slash autocomplete,
      and a relaunch typed after it became «/exitclaude …». So Enter is
      pressed again only while the box still holds the command. Any other
      screen gets Esc and the restart stops, with no relaunch typed.

    A draft left in the input box is cleared first: the exit command would be
    appended to it and the whole line sent to the agent as a prompt. A pane
    already at the shell is left alone (nothing to quit, and the exit command
    would run there); an unreadable one gets nothing typed.
    """
    running, _ = await _agent_state(window_id)
    if running is None:
        return ExitResult(False)  # tmux unreadable: type nothing
    if running is False:
        return ExitResult(True)  # already back at the shell: nothing to quit
    if await session_manager.clear_input(window_id) is False:
        return ExitResult(False)  # a draft that won't go: the exit would join it
    exit_cmd = runtime.exit_command()
    await tmux_manager.send_keys(window_id, exit_cmd)
    deadline = time.monotonic() + EXIT_WAIT_SEC
    flushed = confirmed = False
    notices = 0
    while True:
        running, pane = await _agent_state(window_id)
        if running is False:
            return ExitResult(True)
        prompt = runtime.exit_prompt(pane) if running else None
        if prompt is not None and prompt.kind == "background" and not confirmed:
            if not (stop_background and prompt.stop_selected):
                await _press(window_id, "Escape")
                if stop_background:
                    # «Stop» isn't the highlighted answer: offering the same
                    # button again would only loop back here.
                    return ExitResult(False)
                return ExitResult(False, prompt.items or ("…",))
            await _press(window_id, "Enter")
            confirmed = True
            deadline = time.monotonic() + EXIT_WAIT_SEC
        elif prompt is not None and prompt.kind == "notice" and notices < 2:
            notices += 1
            await _press(window_id, "Escape")
            deadline = time.monotonic() + EXIT_WAIT_SEC
        elif time.monotonic() >= deadline:
            if running and not flushed and runtime.unsent_in_composer(pane, exit_cmd):
                flushed = True
                await _press(window_id, "Enter")
                deadline = time.monotonic() + EXIT_FLUSH_WAIT_SEC
            else:
                if prompt is not None or (running and is_interactive_ui(pane)):
                    await _press(window_id, "Escape")
                logger.warning("Restart: %s did not quit; no relaunch typed", window_id)
                return ExitResult(False)
        await asyncio.sleep(_POLL_SEC)


def restart_refused(
    window_id: str, result: ExitResult, *, fresh: bool
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The message (and button) for a restart that didn't go through."""
    name = session_manager.get_display_name(window_id)
    if result.refused == "busy":
        return tr("commands.restart_busy", name=name), None
    if result.refused == "dialog":
        return tr("restart.dialog_open", name=name), None
    if result.refused == "restarting":
        return tr("restart.in_progress", name=name), None
    if result.refused == "unreadable":
        return tr("restart.unreadable", name=name), None
    if not result.background:
        return tr("restart.not_exited", name=name), None
    items = "\n".join(f"• `{item.replace('`', '')}`" for item in result.background)
    kind = "f" if fresh else "r"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("restart.stop_and_restart_btn"),
                    callback_data=(
                        f"{CB_CMD_RESTART_STOP}{kind}:{window_id[:CALLBACK_WID_MAX]}"
                    ),
                )
            ]
        ]
    )
    return tr("restart.background_work", name=name, items=items), keyboard


async def relaunch_tmux_agent(
    window_id: str,
    runtime: AgentRuntime,
    launch: Callable[[], Awaitable[str]],
    wait_ready: Callable[[str], Awaitable[None]],
    *,
    stop_background: bool = False,
) -> ExitResult:
    """Quit a tmux window's agent and start it again in the same pane.

    ``launch`` returns the launch line once the CLI quit; ``wait_ready`` waits
    for the new CLI's input box. The send lock spans all of it: text typed
    between the exit and the relaunch would run in the shell, and text typed
    while the new CLI boots lands blind. The busy and dialog checks run again
    under the lock, so a message or a second tap that got in first wins.
    """
    if window_id in _restarting:
        return ExitResult(False, refused="restarting")
    _restarting.add(window_id)
    try:
        async with session_manager.send_lock(window_id):
            blocker = _pane_blocker(
                window_id, await tmux_manager.capture_pane(window_id)
            )
            if blocker:
                return ExitResult(False, refused=blocker)
            result = await exit_for_relaunch(
                window_id, runtime, stop_background=stop_background
            )
            if result.exited:
                # Anything left typed at the shell prompt would prefix the
                # launch line.
                await _press(window_id, "C-u")
                await tmux_manager.send_keys(window_id, await launch())
                await wait_ready(window_id)
            return result
    finally:
        _restarting.discard(window_id)


def _runtime_for_topic(user_id: int, thread_id: int) -> AgentRuntime:
    """The CLI this topic last ran, degraded to the default if it's gone."""
    remembered = session_manager.get_remembered_runtime(user_id, thread_id)
    rt = get_runtime(remembered) if remembered else default_runtime()
    if not rt.is_available():
        logger.warning(
            "Revive: runtime %s unavailable for thread %d — using default",
            rt.name,
            thread_id,
        )
        rt = default_runtime()
    return rt


async def resumable(rt: AgentRuntime, session_id: str) -> str:
    """``session_id``, or "" when the CLI has no transcript to resume it from."""
    if session_id and await asyncio.to_thread(rt.has_transcript, session_id) is False:
        logger.info("No transcript for session %s: starting fresh", session_id)
        return ""
    return session_id


async def _session_to_resume(rt: AgentRuntime, cwd: str, old_wid: str | None) -> str:
    """The session id 🔄 should continue, or "" when there is nothing to resume.

    The dead window's own state is the exact answer while it lasts; the stale
    sweep in ``load_session_map`` drops it, so the newest session recorded for
    the folder is the fallback (same conversation for every everyday case —
    one topic works one folder).
    """
    if old_wid:
        ws = session_manager.window_states.get(old_wid)
        if ws and ws.session_id:
            return ws.session_id
    # Another topic on this folder (a sibling) may be the one that talked
    # last: a session a bound topic holds is not this topic's to resume. Only
    # bound ones: the dead window's leftover state holds THIS topic's session.
    bound = {wid for _, _, wid in session_manager.iter_thread_bindings()}
    taken = {
        state.session_id
        for wid, state in session_manager.window_states.items()
        if wid in bound and wid != old_wid and state.session_id
    }
    sessions = await rt.list_sessions(session_manager, cwd)
    return next((s.session_id for s in sessions if s.session_id not in taken), "")


async def revive_topic_agent(
    user_id: int,
    thread_id: int,
    *,
    fresh: bool = False,
    resume_session_id: str | None = None,
) -> tuple[str, str]:
    """Recreate this topic's agent window and bind it. Returns (wid, display).

    ``fresh=True`` (🆕 New session on a dead window) skips the resume and
    starts the same CLI in the same folder with an empty context.
    ``resume_session_id`` names the session to continue when the caller knows
    it better than the dead window's state (boot auto-resume: that state is
    already swept by then).

    Raises :class:`ReviveError` with a user-facing i18n key when the topic has
    no remembered folder, the folder is gone, a hookless runtime already runs
    there, or tmux refuses the window.
    """
    from pathlib import Path

    cwd = session_manager.get_remembered_directory(user_id, thread_id)
    if not cwd or not Path(cwd).is_dir():
        raise ReviveError("restart.nothing_to_revive")

    owner = session_manager.docker_agent_for_dir(cwd)
    if owner:
        raise ReviveError("bot.docker_agent_dir", agent=owner)

    rt = _runtime_for_topic(user_id, thread_id)
    old_wid = session_manager.get_window_for_thread(user_id, thread_id)
    if fresh:
        resume_id = ""
    else:
        resume_id = resume_session_id or await _session_to_resume(rt, cwd, old_wid)
        resume_id = await resumable(rt, resume_id)

    # Same-cwd guard for hookless runtimes: their transcript resolves by cwd
    # ("newest wins"), so a second live window here would make two topics
    # mirror one session.
    if not rt.uses_session_map and await session_manager.has_live_agent_on_cwd(
        rt.name, cwd
    ):
        raise ReviveError("bot.same_dir_conflict", agent=rt.display_name, dir=cwd)

    # One process per session (see bot._create_and_bind_window).
    if resume_id and await session_manager.live_window_for_session(resume_id):
        raise ReviveError("bot.session_already_open")

    ok, message, wname, wid = await tmux_manager.create_window(
        cwd,
        window_name=session_manager.topic_window_name(user_id, thread_id),
        resume_session_id=resume_id or None,
        runtime=rt.name,
    )
    if not ok:
        logger.warning("Revive: create_window failed for %s: %s", cwd, message)
        raise ReviveError("restart.window_failed", err=message[:120])

    session_manager.tag_window_runtime(wid, rt.name, cwd)
    session_manager.record_thread_directory(user_id, thread_id, cwd, runtime=rt.name)
    hook_ok = False
    if rt.uses_session_map:
        hook_ok = await session_manager.wait_for_session_map_entry(
            wid, timeout=15.0 if resume_id else 5.0
        )
    session_manager.bind_thread(user_id, thread_id, wid, window_name=wname)

    if resume_id and rt.uses_session_map:
        # `--resume` makes the SessionStart hook report a NEW session_id while
        # messages keep writing to the ORIGINAL JSONL — pin window_state to the
        # resumed id so the monitor tracks the right transcript (mirrors the
        # resume override in bot._create_and_bind_window).
        ws = session_manager.get_window_state(wid)
        if not hook_ok:
            logger.warning(
                "Revive: hook timed out for %s — pinning session_id=%s cwd=%s",
                wid,
                resume_id,
                cwd,
            )
            ws.session_id = resume_id
            ws.cwd = cwd
            ws.window_name = wname
            session_manager._save_state()
        elif ws.session_id != resume_id:
            ws.session_id = resume_id
            session_manager._save_state()

    logger.info(
        "Revived agent for thread %d: window %s at %s (runtime=%s, resume=%s)",
        thread_id,
        wid,
        cwd,
        rt.name,
        resume_id or "none",
    )
    return wid, wname


async def resume_agents_after_boot() -> None:
    """Relaunch every topic whose window died with tmux, on its own session.

    ``KIKUBOT_AUTO_RESUME_AGENTS``: after a host reboot or a container rebuild
    every agent window is gone, and without this each topic sat dead until
    someone wrote into it and picked the session again. Sequential (one
    claude boot at a time), best-effort per topic. A session met twice — two
    users' bindings on one topic, as before the one-agent-per-topic fix — is
    relaunched once; the other writer then routes to it. Nothing is posted
    to the topics — the conversation just carries on.
    """
    orphans, session_manager.boot_orphans = session_manager.boot_orphans, []
    started: set[str] = set()
    for user_id, thread_id, session_id in orphans:
        if session_id and session_id in started:
            continue
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        if session_manager.get_window_for_thread(user_id, thread_id) or (
            session_manager.topic_owner(chat_id, thread_id, exclude=user_id)
        ):
            continue
        try:
            _, display = await revive_topic_agent(
                user_id, thread_id, resume_session_id=session_id or None
            )
        except ReviveError as e:
            logger.info("Boot resume skipped thread %d: %s", thread_id, e.key)
            continue
        except Exception:
            logger.exception("Boot resume failed for thread %d", thread_id)
            continue
        if session_id:
            started.add(session_id)
        # Silent: a restart should look like nothing happened in the chat.
        logger.info("Boot resume: thread %d back as %s", thread_id, display)


# --- Docker bindings: their agent is a tmux session inside the container ----

# How many earlier conversations the «other sessions» screen offers. One
# container's claude-home holds every sibling's history (33 files on the
# operator's box), so the list is a shortlist by recency, not an archive.
REVIVE_SESSION_ROWS = 6


async def revive_docker_agent(
    window_id: str, *, session_id: str | None, only_if_down: bool = False
) -> None:
    """Start this docker binding's agent again — resuming, or clean.

    ``session_id`` continues that conversation (``claude --resume``);
    ``None`` starts a fresh one with a kikubot-pinned id, the same way a
    sibling is provisioned. The binding and the topic are untouched either
    way — that is the whole point of reviving instead of re-creating: the
    topic keeps its history and its files.

    ``only_if_down``: the automatic callers (poll, wake) decided the agent was
    dead before taking the lock; if it runs by the time they hold it (a
    restart started it meanwhile), nothing is killed.
    """
    from ..docker_driver import docker_driver

    target = session_manager.resolve_docker_target(window_id)
    if target is None:
        raise ReviveError("revive.no_agent")
    if not await docker_driver.is_container_alive(target.agent.container):
        raise ReviveError("revive.container_down", container=target.agent.container)
    if session_id and not is_valid_session_id(session_id):
        # The id rides in a callback payload and ends up on a command line.
        raise ReviveError("revive.bad_session")

    cwd = session_manager.get_window_state(window_id).cwd or "/workspace"
    async with session_manager.send_lock(window_id):
        if only_if_down and await session_manager.docker_agent_running(window_id):
            return
        # A dead session is the normal case here; kill_session tolerates its
        # absence and clears a half-alive one that would refuse the new name.
        await docker_driver.kill_session(
            target.agent.container, session=target.tmux_session
        )
        await asyncio.sleep(1)
        ok = await session_manager.start_docker_agent(
            window_id,
            resume_session_id=session_id,
            new_session_id=None if session_id else str(uuid.uuid4()),
            cwd=cwd,
        )
    if not ok:
        raise ReviveError("revive.start_failed")

    if session_id:
        # `--resume` makes the container's hook report a NEW session id while
        # messages keep landing in the resumed JSONL — point the monitor at
        # the transcript the user actually chose (same override as the tmux
        # revive and the picker's resume path).
        state = session_manager.get_window_state(window_id)
        if state.session_id != session_id:
            state.session_id = session_id
            state.cwd = cwd
            session_manager._save_state()

    logger.info(
        "Revived docker agent %s (container=%s, session=%s, resume=%s)",
        window_id,
        target.agent.container,
        target.tmux_session,
        session_id or "none",
    )


# How long a woken agent gets to draw its input box before the message that
# woke it is typed (a resumed claude boots in ~5-10 s).
WAKE_READY_TIMEOUT = 40.0


async def wake_docker_agent(window_id: str) -> bool:
    """Bring a docker agent that isn't running back on its own conversation.

    For a message arriving in a topic whose agent was stopped (⏹, an idle
    reaper) or died: resume its last session and wait for the TUI, so the
    caller can deliver the message as if nothing happened — no "bring it
    back?" keyboard. True iff the agent is up and ready for input; False
    (container down, no known session, start failed) leaves the caller on
    its normal failure path, which shows the offer.
    """
    from ..terminal_parser import is_tui_ready

    if not session_manager._is_docker_binding(window_id):
        return False
    if await session_manager.docker_agent_running(window_id):
        return False
    last, _ = await docker_revive_options(window_id)
    if last is None:
        return False
    try:
        await revive_docker_agent(
            window_id, session_id=last.session_id, only_if_down=True
        )
    except ReviveError as e:
        logger.info("Wake of %s failed: %s", window_id, e.key)
        return False
    deadline = asyncio.get_running_loop().time() + WAKE_READY_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1.0)
        pane = await session_manager.capture_pane(window_id)
        if pane and is_tui_ready(pane):
            logger.info("Woke %s on session %s", window_id, last.session_id)
            return True
    logger.warning("Woke %s but its TUI never became ready", window_id)
    return False


async def docker_revive_options(
    window_id: str,
) -> tuple[AgentSession | None, list[AgentSession]]:
    """(the topic's own last conversation, other resumable ones).

    Sessions another binding is tracking are dropped: one container's agents
    share a claude-home, and resuming a conversation the parent (or another
    sibling) currently holds would have two agents writing one transcript.
    """
    state = session_manager.window_states.get(window_id)
    last_id = state.session_id if state else ""
    busy = session_manager.session_ids_of_other_bindings(window_id)
    sessions = [
        s
        for s in await session_manager.list_agent_sessions(window_id)
        if s.session_id not in busy
    ]
    last = next((s for s in sessions if s.session_id == last_id), None)
    if last is None and last_id and last_id not in busy:
        # The listing is a shortlist by recency over the whole container, so
        # an agent that sat stopped while its neighbours kept talking drops
        # out of it — and "its own last conversation" then looked unknown:
        # nothing woke it, and the offer lost its ▶️ continue button. The
        # binding's own record is the answer; the listing only decorates it.
        own = await session_manager.resolve_session_for_window(window_id)
        if own is not None and own.session_id == last_id:
            last = own
    return last, [s for s in sessions if s is not last]


def _session_label(session: AgentSession, key: str) -> str:
    """Button label for one resumable conversation (title + age)."""
    from .directory_browser import _relative_time_short

    title = session.summary[:24] + "…" if len(session.summary) > 24 else session.summary
    age = _relative_time_short(session.file_path)
    return tr(key, title=title, age=age) if age else tr(key, title=title, age="")


def build_revive_keyboard(
    last: AgentSession | None, others: list[AgentSession]
) -> InlineKeyboardMarkup:
    """The recovery offer: continue where it stopped, start clean, or choose."""
    rows: list[list[InlineKeyboardButton]] = []
    if last is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    _session_label(last, "revive.btn_continue"),
                    callback_data=f"{CB_DVR_RESUME}{last.session_id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(tr("revive.btn_fresh"), callback_data=CB_DVR_FRESH)]
    )
    if others:
        rows.append(
            [InlineKeyboardButton(tr("revive.btn_earlier"), callback_data=CB_DVR_LIST)]
        )
    return InlineKeyboardMarkup(rows)


def build_revive_session_list(others: list[AgentSession]) -> InlineKeyboardMarkup:
    """«Earlier conversations» — a shortlist by recency, newest first."""
    rows = [
        [
            InlineKeyboardButton(
                _session_label(s, "revive.btn_session"),
                callback_data=f"{CB_DVR_RESUME}{s.session_id}",
            )
        ]
        for s in others[:REVIVE_SESSION_ROWS]
    ]
    rows.append(
        [InlineKeyboardButton(tr("revive.btn_back"), callback_data=CB_DVR_BACK)]
    )
    return InlineKeyboardMarkup(rows)


async def offer_docker_revive(
    bot: Bot, chat_id: int, thread_id: int | None, window_id: str
) -> bool:
    """Post the "your agent isn't running — bring it back" offer. True if sent.

    The one place that message is built, because every way the user meets a
    dead container agent leads here: the 👾 panel (whose pane capture fails),
    a message that couldn't be delivered, and the status poll's own notice.
    Without it the panel was a dead end — the 🔄 button it advertised lives
    *inside* the panel it refused to open (operator report 2026-09-13).
    """
    from ..docker_driver import docker_driver

    name = session_manager.get_display_name(window_id)
    target = session_manager.resolve_docker_target(window_id)
    if target is None:
        return False
    if not await docker_driver.is_container_alive(target.agent.container):
        await safe_send(
            bot,
            chat_id,
            tr("revive.container_down", container=target.agent.container),
            message_thread_id=thread_id,
        )
        return True
    last, others = await docker_revive_options(window_id)
    logger.info(
        "Offering revive for %s (thread=%s, resume candidates=%d%s)",
        window_id,
        thread_id,
        len(others),
        ", last session known" if last else "",
    )
    await safe_send(
        bot,
        chat_id,
        tr("revive.offer", name=name),
        message_thread_id=thread_id,
        reply_markup=build_revive_keyboard(last, others),
    )
    return True
