"""Tests for the inline `status` query: who gets answers, how the topic is
resolved, and the shape of the results."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kikubot.agent_status import AgentStatus
from kikubot.handlers import inline_status

CHAT = -1001234567890
OWNER = 111


class _FakeSM:
    def __init__(self):
        self.bindings = {OWNER: {42: "@5", 43: "@6"}}
        self.chats = {f"{OWNER}:42": CHAT, f"{OWNER}:43": CHAT}

    def get_window_for_thread(self, uid, thread):
        return self.bindings.get(uid, {}).get(thread)

    def topic_owner(self, chat_id, thread, exclude):
        return None

    def resolve_chat_id(self, uid, thread=None):
        return self.chats.get(f"{uid}:{thread}", uid)

    def iter_thread_bindings(self):
        for uid, m in self.bindings.items():
            for t, b in m.items():
                yield uid, t, b

    def get_display_name(self, binding):
        return {"@5": "webapp", "@6": "kikubot"}[binding]


@pytest.fixture
def env(monkeypatch):
    sm = _FakeSM()
    monkeypatch.setattr(inline_status, "session_manager", sm)
    monkeypatch.setattr(inline_status, "is_user_allowed", lambda uid: uid == OWNER)
    monkeypatch.setattr(
        inline_status,
        "build_status",
        lambda binding, reserved=(): AgentStatus(
            name=sm.get_display_name(binding), runtime="claude", ts=1, state="idle"
        ),
    )
    return sm


def _update(monkeypatch, uid, text):
    query = SimpleNamespace(query=text, answer=AsyncMock())
    monkeypatch.setattr(
        inline_status, "effective_user", lambda update: SimpleNamespace(id=uid)
    )
    return SimpleNamespace(inline_query=query), query


def _ctx():
    return SimpleNamespace(application=MagicMock(handlers={}))


def test_parse_target():
    assert inline_status.parse_target(f"status {CHAT}:42") == (CHAT, 42)
    assert inline_status.parse_target("status") is None
    assert inline_status.parse_target("status abc:1") is None


async def test_stranger_gets_nothing_cached_per_user(env, monkeypatch):
    update, query = _update(monkeypatch, 999, f"status {CHAT}:42")
    await inline_status.inline_status_handler(update, _ctx())
    args, kwargs = query.answer.call_args
    assert args[0] == []
    assert kwargs["is_personal"] is True  # a shared cache would reach the owner


async def test_targeted_returns_card_and_json(env, monkeypatch):
    update, query = _update(monkeypatch, OWNER, f"status {CHAT}:42")
    await inline_status.inline_status_handler(update, _ctx())
    results = query.answer.call_args.args[0]
    assert [r.id for r in results] == ["status", "status.v1.json"]
    data = json.loads(results[1].input_message_content.message_text)
    assert data["v"] == 1 and data["state"] == "idle"
    assert query.answer.call_args.kwargs == {"cache_time": 0, "is_personal": True}


async def test_chat_id_must_match_the_binding(env, monkeypatch):
    update, query = _update(monkeypatch, OWNER, "status -100999:42")
    await inline_status.inline_status_handler(update, _ctx())
    assert query.answer.call_args.args[0] == []


async def test_someone_elses_topic_is_not_answered(env, monkeypatch):
    # An allowed user with no binding in that topic, and no topic owner
    # resolution: nothing to show.
    monkeypatch.setattr(inline_status, "is_user_allowed", lambda uid: True)
    update, query = _update(monkeypatch, 222, f"status {CHAT}:42")
    await inline_status.inline_status_handler(update, _ctx())
    assert query.answer.call_args.args[0] == []


async def test_untargeted_lists_cards_filtered_by_name(env, monkeypatch):
    update, query = _update(monkeypatch, OWNER, "status")
    await inline_status.inline_status_handler(update, _ctx())
    assert [r.id for r in query.answer.call_args.args[0]] == ["status:42", "status:43"]

    update, query = _update(monkeypatch, OWNER, "web")
    await inline_status.inline_status_handler(update, _ctx())
    assert [r.title for r in query.answer.call_args.args[0]] == ["webapp · 💤 Idle"]


async def test_a_picked_card_is_not_sent_to_the_agent(monkeypatch):
    """A card chosen from our own inline results lands as the user's message
    «via @bot» — text_handler must drop it, not type it into the agent."""
    from kikubot import bot as bot_mod

    monkeypatch.setattr(bot_mod, "effective_user", lambda u: SimpleNamespace(id=OWNER))
    monkeypatch.setattr(bot_mod, "is_user_allowed", lambda uid: True)
    coalesce = AsyncMock()
    monkeypatch.setattr(bot_mod, "coalesce_text", coalesce)
    get_thread = MagicMock()
    monkeypatch.setattr(bot_mod, "get_thread_id", get_thread)
    message = SimpleNamespace(
        text="👾 proj · Claude Code", via_bot=SimpleNamespace(id=7)
    )
    update = SimpleNamespace(message=message)
    ctx = SimpleNamespace(bot=SimpleNamespace(id=7))
    await bot_mod.text_handler(update, ctx)
    get_thread.assert_not_called()
    coalesce.assert_not_called()
