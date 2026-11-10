"""Tests for /mode: Shift+Tab until the footer shows the target mode."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kikubot.handlers import mode_command as mc

_RULE = "─" * 40
_CYCLE = ["manual", "accept edits", "plan mode", "auto mode"]


def _pane(mode: str) -> str:
    footer = "  ? for shortcuts" if mode == "manual" else f"  ⏵⏵ {mode} on"
    return f"x\n{_RULE}\n❯ \n{_RULE}\n{footer}\n"


class _FakeSM:
    def __init__(self, start=0, cycle=_CYCLE):
        self.i, self.cycle, self.presses = start, cycle, 0

    def resolve_window_for_thread(self, uid, thread):
        return "@5"

    def window_runtime(self, binding):
        return "claude"

    async def capture_pane(self, binding):
        return _pane(self.cycle[self.i])

    def send_lock(self, binding):
        import asyncio

        return asyncio.Lock()

    async def _send_keys_unlocked(self, binding, key, enter=True, literal=True):
        assert key == "BTab"
        self.presses += 1
        self.i = (self.i + 1) % len(self.cycle)
        return True


@pytest.fixture
def run(monkeypatch):
    replies: list[str] = []

    async def fake_reply(message, text, **kw):
        replies.append(text)

    monkeypatch.setattr(mc, "safe_reply", fake_reply)
    monkeypatch.setattr(mc, "effective_user", lambda u: SimpleNamespace(id=1))
    monkeypatch.setattr(mc, "is_user_allowed", lambda uid: True)
    monkeypatch.setattr(mc, "get_thread_id", lambda u: 42)
    monkeypatch.setattr(mc, "PRESS_SETTLE_SEC", 0)

    async def _run(sm, args):
        monkeypatch.setattr(mc, "session_manager", sm)
        await mc.mode_command(
            SimpleNamespace(message=AsyncMock()), SimpleNamespace(args=args)
        )
        return replies

    return _run


async def test_presses_until_target(run):
    sm = _FakeSM(start=0)
    replies = await run(sm, ["plan"])
    assert sm.presses == 2
    assert replies == ["Mode: Plan."]


async def test_already_there_presses_nothing(run):
    sm = _FakeSM(start=3)
    assert await run(sm, ["auto"]) == ["Mode: Auto."]
    assert sm.presses == 0


async def test_unreachable_mode_stops_after_a_cycle(run):
    sm = _FakeSM(start=0, cycle=["manual", "accept edits", "plan mode"])
    replies = await run(sm, ["auto"])
    assert sm.presses == 3  # back where it started: stop, mode unchanged
    assert sm.i == 0
    assert replies[0].startswith("Couldn't switch to Auto")


async def test_no_argument_shows_current_and_choices(run):
    sm = _FakeSM(start=1)
    replies = await run(sm, [])
    assert "Mode now: Accept edits." in replies[0]
    assert "/mode auto" in replies[0]
    assert sm.presses == 0


async def test_never_presses_into_a_dialog(run):
    class _Dialog(_FakeSM):
        async def capture_pane(self, binding):
            return (
                "Do you want to make this edit?\n❯ 1. Yes\n  2. Yes, and allow all edits\n"
                "  3. No\nEsc to cancel · Tab to amend\n"
            )

    sm = _Dialog()
    replies = await run(sm, ["plan"])
    assert sm.presses == 0
    assert replies[0].startswith("Can't see the agent's mode")


async def test_bypass_footer_under_a_user_status_line(run):
    class _Bypass(_FakeSM):
        async def capture_pane(self, binding):
            return (
                f"x\n{_RULE}\n❯ \n{_RULE}\n  ctx 6%\n  Opus 5.5\n"
                "  ⏵⏵ bypass permissions on\n"
            )

    replies = await run(_Bypass(), [])
    assert "Mode now: Bypass permissions." in replies[0]
