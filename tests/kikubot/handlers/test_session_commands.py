"""Tests for the session-action commands a client's «Session» menu sends:
/bgtask presses Ctrl+B only while a turn runs, never into a dialog;
/sibling and /worktree open the same naming prompts as the panel's ➕ and 🌳."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kikubot.handlers import commands

RULE = "─" * 60
WORKING = f"✻ Cooking… (12s · esc to interrupt)\n{RULE}\n❯ \n{RULE}\n"
IDLE = f"● done\n{RULE}\n❯ \n{RULE}\n"
DIALOG = (
    "● Bash(rm -rf build)\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n"
    " Esc to cancel · Tab to amend\n"
)


def _update():
    message = SimpleNamespace()
    return SimpleNamespace(message=message), message


@pytest.fixture
def sm(monkeypatch):
    sm = MagicMock()
    sm.resolve_window_for_thread.return_value = "@5"
    sm.get_display_name.return_value = "proj"
    sm.agent_supports.return_value = True
    sm.send_keys = AsyncMock(return_value=True)
    monkeypatch.setattr(commands, "session_manager", sm)
    monkeypatch.setattr(commands, "effective_user", lambda u: SimpleNamespace(id=1))
    monkeypatch.setattr(commands, "is_user_allowed", lambda uid: True)
    monkeypatch.setattr(commands, "get_thread_id", lambda u: 42)
    return sm


@pytest.mark.parametrize(
    ("pane", "working", "sent", "reply_key"),
    [
        (WORKING, True, True, "bgtask.sent"),
        (IDLE, False, False, "bgtask.nothing_running"),
        (DIALOG, True, False, "cb.agent_dialog_answer_first"),
        (None, False, False, "cb.agent_screen_unreadable"),
    ],
)
async def test_bgtask_only_while_a_turn_runs(sm, pane, working, sent, reply_key):
    sm.capture_pane = AsyncMock(return_value=pane)
    sm.is_agent_working.return_value = working
    update, _ = _update()
    with patch.object(commands, "safe_reply", new=AsyncMock()) as reply:
        await commands.bgtask_command(update, MagicMock())
    assert sm.send_keys.await_count == (1 if sent else 0)
    if sent:
        assert sm.send_keys.await_args.args[:2] == ("@5", "C-b")
    assert reply.await_args.args[1] == commands.tr(reply_key, name="proj")


async def test_bgtask_refused_for_a_cli_without_background(sm):
    sm.agent_supports.return_value = False
    update, _ = _update()
    with patch.object(commands, "safe_reply", new=AsyncMock()):
        await commands.bgtask_command(update, MagicMock())
    sm.send_keys.assert_not_awaited()


async def test_sibling_command_opens_the_naming_prompt(sm):
    sm.can_offer_sibling.return_value = True
    update, _ = _update()
    ctx = MagicMock()
    with patch("kikubot.handlers.siblings.ask_sibling_name", new=AsyncMock()) as ask:
        await commands.sibling_command(update, ctx)
    ask.assert_awaited_once_with(ctx, 1, 42, "@5")


async def test_worktree_command_refuses_a_non_repo(sm):
    sm.can_offer_worktree.return_value = True
    update, _ = _update()
    with (
        patch("kikubot.handlers.worktrees.worktree_refusal", return_value="no repo"),
        patch("kikubot.handlers.worktrees.ask_worktree_task", new=AsyncMock()) as ask,
        patch.object(commands, "safe_reply", new=AsyncMock()) as reply,
    ):
        await commands.worktree_command(update, MagicMock())
    ask.assert_not_awaited()
    assert reply.await_args.args[1] == "no repo"
