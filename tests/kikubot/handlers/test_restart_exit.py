"""Tests for exit_for_relaunch: a restart never confirms the CLI's exit
screens blind (Claude Code preselects «Exit and stop tasks») and types the
relaunch only after the CLI really quit."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Message

from kikubot.handlers import agent_restart
from kikubot.handlers.agent_restart import ExitResult, exit_for_relaunch
from kikubot.runtimes import get_runtime

RULE = "─" * 100
IDLE = f"done\n{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on\n"
EXIT_TYPED = f"done\n{RULE}\n❯ /exit\n{RULE}\n  ⏵⏵ auto mode on\n"


def _background(cursor_on: int = 1) -> str:
    options = ["1. Exit and stop tasks", "2. Move to background and exit", "3. Stay"]
    rows = [
        f"  ❯ {opt}" if i == cursor_on else f"    {opt}"
        for i, opt in enumerate(options, 1)
    ]
    return "\n".join(
        [
            "done",
            "❯ /exit",
            RULE,
            RULE,
            "  Background work is running",
            "  The following will stop when you exit:",
            "  shell · npm run dev",
            "  scheduled task · Runs once in 2h · Daily check",
            *rows,
            "  Enter to confirm · Esc to cancel",
            "",
        ]
    )


DRAFTS = (
    f"done\n❯ /exit\n{RULE}\n{RULE}\n  Feedback drafts\n    This session\n"
    "  ❯ Model did something   bug · 2d\n"
    "  Enter to review · d to discard · Esc to close\n"
)


class _FakeTmux:
    """A pane whose state follows the keys pressed so far."""

    def __init__(self, react):
        self.keys: list[str] = []
        self._react = react

    async def send_keys(self, wid, text, enter=True, literal=True):
        self.keys.append(text)
        return True

    async def find_window_by_id(self, wid):
        return object()

    async def is_agent_running(self, w):
        return self._react(self.keys)[0]

    async def capture_pane(self, wid):
        return self._react(self.keys)[1]


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 0.0}
    monkeypatch.setattr(agent_restart.time, "monotonic", lambda: now["t"])

    async def _tick(sec):
        now["t"] += sec

    monkeypatch.setattr(agent_restart.asyncio, "sleep", _tick)
    return now


async def _run(react, *, window=True, **kw):
    tm = _FakeTmux(react)
    if not window:
        tm.find_window_by_id = AsyncMock(return_value=None)  # type: ignore[method-assign]

    async def clear_input(wid):
        # Stands in for SessionManager.clear_input: one mark per draft cleared.
        if get_runtime("claude").typed_input(react(tm.keys)[1]):
            tm.keys.append("<clear>")
        return True

    with (
        patch.object(agent_restart, "tmux_manager", tm),
        patch.object(agent_restart.session_manager, "clear_input", clear_input),
    ):
        result = await exit_for_relaunch("@5", get_runtime("claude"), **kw)
    return result, tm.keys


@pytest.mark.asyncio
async def test_plain_exit(clock):
    result, keys = await _run(lambda k: (True, IDLE) if not k else (False, ""))
    assert result == ExitResult(True)
    assert keys == ["/exit"]


@pytest.mark.asyncio
async def test_a_pane_already_at_the_shell_gets_nothing_typed(clock):
    # The exit command would run there as a shell command.
    result, keys = await _run(lambda k: (False, ""))
    assert result == ExitResult(True) and keys == []


@pytest.mark.asyncio
async def test_an_unreadable_pane_gets_nothing_typed(clock):
    result, keys = await _run(lambda k: (True, IDLE), window=False)
    assert result == ExitResult(False) and keys == []


@pytest.mark.asyncio
async def test_swallowed_enter_is_pressed_again(clock):
    # /exit sits in the box until a second Enter submits it.
    def react(keys):
        if "/exit" not in keys:
            return True, IDLE
        return (False, "") if "Enter" in keys else (True, EXIT_TYPED)

    result, keys = await _run(react)
    assert result.exited
    assert keys == ["/exit", "Enter"]


@pytest.mark.asyncio
async def test_a_draft_is_cleared_before_the_exit_command(clock):
    # Typed onto a leftover draft, /exit would go out as «draft/exit».
    draft = f"done\n{RULE}\n❯ half a message\n{RULE}\n  ⏵⏵ auto mode on\n"

    def react(keys):
        if "/exit" in keys:
            return False, ""
        return (True, IDLE) if "<clear>" in keys else (True, draft)

    result, keys = await _run(react)
    assert result.exited
    assert keys == ["<clear>", "/exit"]


@pytest.mark.asyncio
async def test_a_draft_that_wont_clear_stops_the_restart(clock):
    tm = _FakeTmux(lambda k: (True, IDLE))
    with (
        patch.object(agent_restart, "tmux_manager", tm),
        patch.object(
            agent_restart.session_manager,
            "clear_input",
            AsyncMock(return_value=False),
        ),
    ):
        result = await exit_for_relaunch("@5", get_runtime("claude"))
    assert result == ExitResult(False) and tm.keys == []


@pytest.mark.asyncio
async def test_background_work_cancels_the_exit(clock):
    def react(keys):
        return (True, IDLE) if "Escape" in keys else (True, _background())

    result, keys = await _run(react)
    assert not result.exited
    assert result.background == (
        "shell · npm run dev",
        "scheduled task · Runs once in 2h · Daily check",
    )
    assert keys == ["/exit", "Escape"]  # never the preselected Enter


@pytest.mark.asyncio
async def test_background_work_stopped_when_the_user_chose_it(clock):
    def react(keys):
        return (False, "") if "Enter" in keys else (True, _background())

    result, keys = await _run(react, stop_background=True)
    assert result.exited
    assert keys == ["/exit", "Enter"]


@pytest.mark.asyncio
async def test_stop_needs_the_cursor_on_stop(clock):
    # The highlighted answer isn't «stop»: don't guess, cancel — and don't
    # offer the same «stop and restart» button again, which would loop.
    def react(keys):
        return (True, IDLE) if "Escape" in keys else (True, _background(3))

    result, keys = await _run(react, stop_background=True)
    assert result == ExitResult(False)
    assert keys == ["/exit", "Escape"]


@pytest.mark.asyncio
async def test_feedback_drafts_closed_and_exit_goes_on(clock):
    def react(keys):
        return (False, "") if "Escape" in keys else (True, DRAFTS)

    result, keys = await _run(react)
    assert result.exited
    assert keys == ["/exit", "Escape"]


@pytest.mark.asyncio
async def test_stuck_without_a_known_screen_gives_up_without_enter(clock):
    result, keys = await _run(lambda k: (True, IDLE))
    assert result == ExitResult(False)
    assert keys == ["/exit"]


@pytest.mark.asyncio
async def test_quoted_dialog_in_the_transcript_is_not_a_screen(clock):
    # The dialog's words above a live input box are a transcript quoting it.
    quoted = _background() + IDLE
    result, keys = await _run(lambda k: (True, quoted))
    assert result == ExitResult(False)
    assert keys == ["/exit"]


@pytest.mark.asyncio
async def test_panel_restart_refused_types_no_relaunch_and_offers_the_choice(
    monkeypatch,
):
    from kikubot.handlers import callbacks

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    query = MagicMock()
    query.answer = AsyncMock()
    query.message = MagicMock(spec=Message)
    tm = MagicMock()
    tm.find_window_by_id = AsyncMock(return_value=MagicMock(window_name="proj"))
    tm.capture_pane = AsyncMock(return_value=IDLE)
    tm.send_keys = AsyncMock()
    reply = AsyncMock()
    with (
        patch.object(callbacks, "session_manager") as sm,
        patch.object(callbacks, "tmux_manager", tm),
        patch.object(agent_restart, "tmux_manager", tm),
        patch.object(agent_restart, "session_manager", sm),
        patch.object(
            agent_restart,
            "exit_for_relaunch",
            new=AsyncMock(return_value=ExitResult(False, ("shell · npm run dev",))),
        ),
        patch.object(callbacks, "safe_reply", new=reply),
        patch.object(callbacks, "_cmd_refresh_photo", new=AsyncMock()),
    ):
        sm._is_docker_binding.return_value = False
        sm.is_agent_working.return_value = False
        sm.capture_pane = AsyncMock(return_value=IDLE)
        sm.get_display_name.return_value = "proj"
        sm.get_window_state.return_value = MagicMock(session_id="", runtime="claude")
        await callbacks._restart_agent(query, "@5", fresh=False)

    tm.send_keys.assert_not_called()  # no relaunch typed
    text = reply.await_args.args[1]
    keyboard = reply.await_args.kwargs["reply_markup"]
    assert "npm run dev" in text
    assert keyboard.inline_keyboard[0][0].callback_data == "cm:rstop:r:@5"


def test_restart_stop_payload_is_guarded():
    from kikubot.handlers.callbacks import _guarded_wid

    assert _guarded_wid("cm:rstop:f:@12") == "@12"


class _Lockable:
    """session_manager stand-in with a real per-binding lock."""

    def __init__(self, working=False, pane=IDLE):
        self.lock = asyncio.Lock()
        self.working = working
        self.pane = pane

    async def capture_pane(self, wid):
        return self.pane

    def _is_docker_binding(self, wid):
        return wid.startswith("docker:")

    def window_runtime(self, wid):
        return "claude"

    def send_lock(self, wid):
        return self.lock

    def is_agent_working(self, wid, pane):
        return self.working


async def _relaunch(sm, pane, *, exits=True, wait_ready=None):
    tm = MagicMock()
    tm.capture_pane = AsyncMock(return_value=pane)
    tm.send_keys = AsyncMock()
    launched = AsyncMock(return_value="claude --name proj")
    ready = wait_ready or AsyncMock()
    with (
        patch.object(agent_restart, "session_manager", sm),
        patch.object(agent_restart, "tmux_manager", tm),
        patch.object(
            agent_restart,
            "exit_for_relaunch",
            new=AsyncMock(return_value=ExitResult(exits)),
        ),
    ):
        result = await agent_restart.relaunch_tmux_agent(
            "@5", get_runtime("claude"), launched, ready
        )
    return result, tm, ready


@pytest.mark.asyncio
async def test_relaunch_holds_the_lock_until_the_new_cli_is_ready():
    sm = _Lockable()
    seen = {}

    async def ready(wid):
        seen["locked"] = sm.lock.locked()
        seen["restarting"] = agent_restart.is_restarting(wid)

    result, tm, _ = await _relaunch(sm, IDLE, wait_ready=ready)
    assert result.exited
    assert tm.send_keys.await_args.args == ("@5", "claude --name proj")
    assert seen == {"locked": True, "restarting": True}
    assert not agent_restart.is_restarting("@5")


@pytest.mark.asyncio
async def test_relaunch_rechecks_busy_under_the_lock():
    # A message got in between the tap and the lock: the agent is busy now.
    result, tm, ready = await _relaunch(_Lockable(working=True), IDLE)
    assert result == ExitResult(False, refused="busy")
    tm.send_keys.assert_not_called()
    ready.assert_not_called()


@pytest.mark.asyncio
async def test_relaunch_refuses_while_a_dialog_waits():
    result, tm, _ = await _relaunch(_Lockable(), _background())
    assert result.refused == "dialog"
    tm.send_keys.assert_not_called()


@pytest.mark.asyncio
async def test_a_second_restart_is_refused_while_one_runs():
    agent_restart._restarting.add("@5")
    try:
        result, tm, _ = await _relaunch(_Lockable(), IDLE)
    finally:
        agent_restart._restarting.discard("@5")
    assert result.refused == "restarting"
    tm.capture_pane.assert_not_called()


@pytest.mark.asyncio
async def test_no_launch_when_the_cli_did_not_quit():
    result, tm, ready = await _relaunch(_Lockable(), IDLE, exits=False)
    assert not result.exited
    tm.send_keys.assert_not_called()
    ready.assert_not_called()
    assert not agent_restart.is_restarting("@5")


@pytest.mark.asyncio
async def test_panel_alerts_on_an_open_dialog():
    from kikubot.handlers import callbacks

    query = MagicMock()
    query.answer = AsyncMock()
    with patch.object(agent_restart, "session_manager", _Lockable(pane=_background())):
        assert await callbacks._answer_restart_blocked(query, "@5", fresh=False)
    assert query.answer.await_args.kwargs["show_alert"] is True
    assert "dialog" in query.answer.await_args.args[0]


def test_claude_transcript_lookup(tmp_path, monkeypatch):
    from kikubot.runtimes import config as rcfg

    sid = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(rcfg, "claude_projects_path", tmp_path)
    rt = get_runtime("claude")
    assert rt.has_transcript(sid) is False
    (tmp_path / "-home-user-proj").mkdir()
    (tmp_path / "-home-user-proj" / f"{sid}.jsonl").write_text("{}\n")
    assert rt.has_transcript(sid) is True
    assert rt.has_transcript("not a session id") is None


@pytest.mark.asyncio
async def test_resume_without_a_transcript_starts_fresh(monkeypatch):
    rt = get_runtime("claude")
    monkeypatch.setattr(rt, "has_transcript", lambda sid: False)
    assert await agent_restart.resumable(rt, "x") == ""
    monkeypatch.setattr(rt, "has_transcript", lambda sid: None)
    assert await agent_restart.resumable(rt, "x") == "x"


# --- docker bindings -------------------------------------------------------


def _docker_sm(pane=IDLE, running=True):
    from types import SimpleNamespace

    sm = MagicMock()
    sm._is_docker_binding.side_effect = lambda v: v.startswith("docker:")
    sm.capture_pane = AsyncMock(return_value=pane)
    sm.is_agent_working.return_value = False
    sm.window_runtime.return_value = "claude"
    sm.send_lock.return_value = asyncio.Lock()
    sm.docker_agent_running = AsyncMock(return_value=running)
    sm.start_docker_agent = AsyncMock(return_value=True)
    sm.get_window_state.return_value = SimpleNamespace(cwd="/workspace")
    sm.resolve_docker_target.return_value = SimpleNamespace(
        agent=SimpleNamespace(container="ctn"), tmux_session="claude"
    )
    sm.get_display_name.return_value = "assistant"
    return sm


@pytest.mark.asyncio
async def test_docker_restart_reports_background_shells():
    footer = f"done\n{RULE}\n❯ \n{RULE}\n  ⏵⏵ auto mode on · 2 shells\n"
    with patch.object(agent_restart, "session_manager", _docker_sm(footer)):
        result = await agent_restart.restart_precheck("docker:a")
    assert result is not None and not result.refused
    assert result.background == ("2 background shell(s)",)


@pytest.mark.asyncio
async def test_docker_restart_without_a_session_pins_a_new_id():
    import kikubot.docker_driver as dd

    sm = _docker_sm()
    with (
        patch.object(agent_restart, "session_manager", sm),
        patch.object(dd.docker_driver, "kill_session", new=AsyncMock()),
        patch.object(agent_restart.asyncio, "sleep", new=AsyncMock()),
    ):
        assert await agent_restart.restart_docker_agent("docker:a/side", None)
    kwargs = sm.start_docker_agent.await_args.kwargs
    assert kwargs["resume_session_id"] is None
    assert kwargs["new_session_id"]  # pinned, so the sibling stays trackable
    assert not agent_restart.is_restarting("docker:a/side")


@pytest.mark.asyncio
async def test_automatic_revive_leaves_a_running_agent_alone():
    import kikubot.docker_driver as dd

    sm = _docker_sm(running=True)
    kill = AsyncMock()
    with (
        patch.object(agent_restart, "session_manager", sm),
        patch.object(
            dd.docker_driver, "is_container_alive", new=AsyncMock(return_value=True)
        ),
        patch.object(dd.docker_driver, "kill_session", new=kill),
    ):
        await agent_restart.revive_docker_agent(
            "docker:a", session_id=None, only_if_down=True
        )
    kill.assert_not_awaited()
    sm.start_docker_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_revive_offer_does_not_touch_a_running_agent():
    from kikubot.handlers import callbacks

    query = MagicMock()
    query.answer = AsyncMock()
    revive = AsyncMock()
    edit = AsyncMock()
    with (
        patch.object(callbacks, "session_manager", _docker_sm(running=True)),
        patch.object(callbacks, "_revive_target", return_value="docker:a"),
        patch.object(agent_restart, "revive_docker_agent", new=revive),
        patch.object(callbacks, "safe_edit", new=edit),
    ):
        await callbacks._handle_dv_revive(
            query, "dv:new", MagicMock(), MagicMock(), MagicMock()
        )
    revive.assert_not_awaited()
    assert "already running" in edit.await_args.args[1]


@pytest.mark.asyncio
async def test_kill_command_on_docker_keeps_the_binding():
    from kikubot.handlers import commands

    update = MagicMock()
    update.message = MagicMock()
    sm = _docker_sm()
    sm.get_window_for_thread.return_value = "docker:a"
    sm.stop_docker_agent = AsyncMock(return_value=True)
    reply = AsyncMock()
    with (
        patch.object(commands, "session_manager", sm),
        patch.object(commands, "effective_user", return_value=MagicMock(id=1)),
        patch.object(commands, "is_user_allowed", return_value=True),
        patch.object(commands, "get_thread_id", return_value=7),
        patch.object(commands, "safe_reply", new=reply),
    ):
        await commands.kill_command(update, MagicMock())
    sm.stop_docker_agent.assert_awaited_once_with("docker:a")
    sm.unbind_thread.assert_not_called()
    sm.forget_thread_runtime.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "revived"),
    [("cm:cfm:restart:@5", True), ("cm:cfm:fresh:@5", True), ("cm:clear:@5", False)],
)
async def test_unbound_topic_panel_revives_from_memory(data, revived):
    """After the reaper unbound the topic, 🔄 on its old panel revives the
    topic's agent; the stale window id (maybe another project's now) is
    never acted on."""
    from kikubot.handlers import callbacks

    update = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    update.effective_chat.type = "private"
    revive = AsyncMock()
    restart = AsyncMock()
    with (
        patch.object(callbacks, "effective_user", return_value=MagicMock(id=1)),
        patch.object(callbacks.config, "is_user_allowed", return_value=True),
        patch.object(callbacks, "get_thread_id", return_value=7),
        patch.object(callbacks, "session_manager") as sm,
        patch.object(callbacks, "_revive_from_panel", new=revive),
        patch.object(callbacks, "_restart_agent", new=restart),
    ):
        sm.resolve_window_for_thread.return_value = None
        await callbacks.callback_handler(update, MagicMock())
    assert revive.await_count == (1 if revived else 0)
    restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_revive_fallback_skips_a_session_another_topic_holds():
    from types import SimpleNamespace

    sm = MagicMock()
    sm.iter_thread_bindings.return_value = [(1, 8, "@9")]  # the sibling topic
    sm.window_states = {
        "@9": SimpleNamespace(session_id="sib"),
        "@3": SimpleNamespace(session_id="mine"),  # this topic's dead window
    }
    rt = MagicMock()
    rt.list_sessions = AsyncMock(
        return_value=[SimpleNamespace(session_id=i) for i in ("sib", "mine", "old")]
    )
    with patch.object(agent_restart, "session_manager", sm):
        assert await agent_restart._session_to_resume(rt, "/p", None) == "mine"


def test_unreadable_tmux_pane_blocks_a_restart_docker_does_not():
    from kikubot.handlers.agent_restart import restart_blocker

    assert restart_blocker("@5", None) == "unreadable"
    # A docker agent without a pane is down: the restart is the fix.
    assert restart_blocker("docker:assistant", None) == ""


def test_stop_button_payload_fits_and_names_the_binding():
    from kikubot.handlers.agent_restart import restart_refused
    from kikubot.handlers.callback_data import CALLBACK_WID_MAX

    wid = "docker:" + "a" * 70
    _, keyboard = restart_refused(wid, ExitResult(False, ("shell · x",)), fresh=True)
    assert keyboard is not None
    data = keyboard.inline_keyboard[0][0].callback_data
    assert data == f"cm:rstop:f:{wid[:CALLBACK_WID_MAX]}" and len(data) <= 64


@pytest.mark.asyncio
async def test_panel_tap_on_an_unreadable_pane_is_answered(monkeypatch):
    """Every refusal reason maps to an alert: a tmux read failure used to
    raise KeyError and leave the tap spinning."""
    from kikubot.handlers import callbacks

    query = MagicMock()
    query.answer = AsyncMock()
    with patch.object(
        agent_restart,
        "restart_precheck",
        new=AsyncMock(return_value=ExitResult(False, refused="unreadable")),
    ):
        assert await callbacks._answer_restart_blocked(query, "@5", fresh=False)
    assert query.answer.await_args.kwargs.get("show_alert") is True
    assert "read" in query.answer.await_args.args[0]
