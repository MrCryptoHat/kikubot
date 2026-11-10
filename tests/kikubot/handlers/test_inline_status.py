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


# --- screen ---------------------------------------------------------------


def test_parse_screen_target():
    assert inline_status.parse_screen_target(f"screen {CHAT}:42") == (CHAT, 42, 0)
    assert inline_status.parse_screen_target(f"screen {CHAT}:42 back=300") == (
        CHAT,
        42,
        300,
    )
    assert inline_status.parse_screen_target(f"screen {CHAT}:42 back=999999")[2] == (
        inline_status.MAX_BACK
    )
    assert inline_status.parse_screen_target(f"status {CHAT}:42") is None
    assert inline_status.parse_screen_target("screen") is None


def _screen_env(env, monkeypatch, screen):
    from kikubot.handlers import pane_cache

    calls = []

    async def capture_screen(binding, *, back=0):
        calls.append((binding, back))
        return screen

    env.capture_screen = capture_screen
    monkeypatch.setattr(pane_cache, "_PANEL_BY_TOPIC", {(OWNER, 42): 900})
    return calls


async def test_screen_answers_meta_and_chunks(env, monkeypatch):
    from kikubot.pane_screen import PaneScreen

    screen = PaneScreen("\x1b[1m  hi\x1b[0m\nrow", 80, 2, (1, 1), 0, 10)
    calls = _screen_env(env, monkeypatch, screen)
    update, query = _update(monkeypatch, OWNER, f"screen {CHAT}:42 back=5")
    await inline_status.inline_status_handler(update, _ctx())
    assert calls == [("@5", 5)]
    results = query.answer.call_args.args[0]
    assert [r.id for r in results] == ["screen", "screen.1"]
    meta = json.loads(results[0].input_message_content.message_text)
    assert meta["panel"] == 900 and meta["parts"] == 1 and meta["cursor"] == [1, 1]
    chunk = json.loads(results[1].input_message_content.message_text)
    assert chunk == {"d": screen.text}
    assert query.answer.call_args.kwargs == {"cache_time": 0, "is_personal": True}


async def test_screen_of_a_foreign_topic_is_empty(env, monkeypatch):
    calls = _screen_env(env, monkeypatch, None)
    update, query = _update(monkeypatch, OWNER, "screen -100999:42")
    await inline_status.inline_status_handler(update, _ctx())
    assert query.answer.call_args.args[0] == []
    assert calls == []  # never captured


async def test_screen_stranger_gets_nothing(env, monkeypatch):
    calls = _screen_env(env, monkeypatch, None)
    update, query = _update(monkeypatch, 999, f"screen {CHAT}:42")
    await inline_status.inline_status_handler(update, _ctx())
    assert query.answer.call_args.args[0] == [] and calls == []


async def test_screen_retries_smaller_when_refused(env, monkeypatch):
    from telegram.error import BadRequest

    from kikubot.pane_screen import PaneScreen

    history = ["\x1b[32m" + "x" * 95 for _ in range(3000)]
    screen = PaneScreen("\n".join(history + ["row"]), 100, 1, None, 3000, 3000)
    _screen_env(env, monkeypatch, screen)
    update, query = _update(monkeypatch, OWNER, f"screen {CHAT}:42 back=3000")
    query.answer.side_effect = [BadRequest("too big"), None]
    await inline_status.inline_status_handler(update, _ctx())
    first, second = query.answer.call_args_list
    assert len(first.args[0]) == inline_status.MAX_PARTS + 1
    assert len(second.args[0]) <= inline_status._SCREEN_RETRY_PARTS + 1
    meta = json.loads(second.args[0][0].input_message_content.message_text)
    assert meta["back"] < 3000


# --- keys -----------------------------------------------------------------


def test_parse_keys_query():
    from kikubot.handlers.inline_keys import parse_keys_query

    assert parse_keys_query(f"keys {CHAT}:42 7 Up =hi Enter .") == (
        CHAT,
        42,
        7,
        ["Up", "=hi", "Enter", "."],
    )
    assert parse_keys_query(f"keys {CHAT}:42 Up .") is None  # no seq
    assert parse_keys_query(f"screen {CHAT}:42") is None


@pytest.fixture
def keys_env(env, monkeypatch):
    from kikubot.handlers import agent_restart, inline_keys, pane_cache
    from kikubot.pane_screen import PaneScreen

    monkeypatch.setattr(inline_keys, "_last_seq", {})
    monkeypatch.setattr(inline_keys, "session_manager", env)
    monkeypatch.setattr(inline_keys, "CHANGE_WAIT", 0.05)
    monkeypatch.setattr(pane_cache, "_PANEL_BY_TOPIC", {})
    monkeypatch.setattr(agent_restart, "_restarting", set())
    screens = iter([PaneScreen(f"frame {i}\n", 80, 1, (0, 0), 0, 0) for i in range(10)])
    sends: list[tuple[str, list]] = []

    async def capture_screen(binding, *, back=0):
        return next(screens)

    async def send_key_tokens(binding, tokens):
        sends.append((binding, tokens))
        return len(tokens), None

    env.capture_screen = capture_screen
    env.send_key_tokens = send_key_tokens
    return sends


async def _keys(monkeypatch, text):
    update, query = _update(monkeypatch, OWNER, text)
    await inline_status.inline_status_handler(update, _ctx())
    results = query.answer.call_args.args[0]
    meta = (
        json.loads(results[0].input_message_content.message_text) if results else None
    )
    return results, meta


async def test_keys_sends_and_answers_the_screen_after(keys_env, monkeypatch):
    from kikubot.terminal_keys import KeyToken

    results, meta = await _keys(monkeypatch, f"keys {CHAT}:42 5 Down =ok Enter .")
    assert keys_env == [
        (
            "@5",
            [KeyToken("Down", False), KeyToken("ok", True), KeyToken("Enter", False)],
        )
    ]
    assert meta["seq"] == 5 and meta["sent"] == 3 and "error" not in meta
    chunk = json.loads(results[1].input_message_content.message_text)["d"]
    assert chunk != "frame 0\n"  # the capture after the keys, not before


async def test_keys_stale_or_repeated_seq_sends_nothing(keys_env, monkeypatch):
    await _keys(monkeypatch, f"keys {CHAT}:42 5 Up .")
    _, meta = await _keys(monkeypatch, f"keys {CHAT}:42 5 Up .")
    assert meta["error"] == "stale" and meta["sent"] == 0
    _, meta = await _keys(monkeypatch, f"keys {CHAT}:42 4 Up .")
    assert meta["error"] == "stale"
    assert len(keys_env) == 1


async def test_keys_bad_token_sends_nothing(keys_env, monkeypatch):
    _, meta = await _keys(monkeypatch, f"keys {CHAT}:42 1 Enter =ls .")
    assert meta["error"] == "bad-key" and meta["sent"] == 0
    assert keys_env == []


async def test_keys_during_a_restart_send_nothing(keys_env, monkeypatch):
    from kikubot.handlers import agent_restart

    agent_restart._restarting.add("@5")
    _, meta = await _keys(monkeypatch, f"keys {CHAT}:42 1 Up .")
    assert meta["error"] == "not-running" and keys_env == []


async def test_keys_to_a_foreign_topic_is_empty(keys_env, monkeypatch):
    results, _ = await _keys(monkeypatch, "keys -100999:42 1 Up .")
    assert results == [] and keys_env == []


async def test_keys_stranger_gets_nothing(keys_env, monkeypatch):
    update, query = _update(monkeypatch, 999, f"keys {CHAT}:42 1 Up .")
    await inline_status.inline_status_handler(update, _ctx())
    assert query.answer.call_args.args[0] == [] and keys_env == []


def test_keys_seq_survives_a_bot_restart(monkeypatch, tmp_path):
    """A client resends an unanswered query with the same seq; one that waited
    out a bot restart must still be `stale`, not typed a second time."""
    from kikubot import utils
    from kikubot.handlers import inline_keys

    monkeypatch.setattr(inline_keys, "kikubot_dir", lambda: tmp_path)
    monkeypatch.setattr(
        inline_keys, "schedule_async_json_write", utils.atomic_write_json
    )
    monkeypatch.setattr(inline_keys, "_last_seq", None)
    inline_keys._remember_seq((OWNER, CHAT, 42), 1794328950962)
    monkeypatch.setattr(inline_keys, "_last_seq", None)  # the restart
    assert inline_keys._seqs() == {(OWNER, CHAT, 42): 1794328950962}

    # A row in the older shape is skipped, the rest kept.
    (tmp_path / "keys_seq.json").write_text('{"1:2": 5, "1:-100:3": 7}')
    monkeypatch.setattr(inline_keys, "_last_seq", None)
    assert inline_keys._seqs() == {(1, -100, 3): 7}

    (tmp_path / "keys_seq.json").write_text("not json")
    monkeypatch.setattr(inline_keys, "_last_seq", None)
    assert inline_keys._seqs() == {}  # unreadable: start empty, don't crash


async def test_keys_seq_far_ahead_of_the_clock_is_refused(keys_env, monkeypatch):
    """One bogus seq would leave every later query `stale` for good."""
    _, meta = await _keys(monkeypatch, f"keys {CHAT}:42 99999999999999999 Up .")
    assert meta["error"] == "bad-key" and keys_env == []
    _, meta = await _keys(monkeypatch, f"keys {CHAT}:42 5 Up .")
    assert "error" not in meta  # nothing was recorded
