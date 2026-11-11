"""Tests for TmuxManager's tmux commands — argv shapes, and the real thing.

The unit half pins argv with a fake tmux: literal sends must carry `--` (text
starting with "-" is otherwise eaten as tmux flags and the send fails), and
session-level commands must address the session by its `$N` id. The real half
runs each operation against a throwaway tmux server, when tmux is installed.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from kikubot import tmux_manager as tm_mod
from kikubot.config import config
from kikubot.tmux_manager import TmuxManager, TmuxResult

TMUX = shutil.which("tmux")
OK = TmuxResult(0, "", "")


def _sessions(*pairs: tuple[str, str]) -> TmuxResult:
    """A list-sessions answer in the manager's own format."""
    rows = (
        f"{sid}{tm_mod._FIELD_SEP}{name}{tm_mod._RECORD_END}\n" for sid, name in pairs
    )
    return TmuxResult(0, "".join(rows), "")


@pytest.fixture
def fake_tmux(monkeypatch):
    """TmuxManager on a scripted tmux; sleeps neutered. Yields (manager, calls, answers).

    ``answers`` maps a tmux subcommand to its result (default: success).
    """
    manager = TmuxManager(session_name="test")
    calls: list[tuple[str, ...]] = []
    answers: dict[str, TmuxResult] = {}

    def tmux(*args: str) -> TmuxResult:
        calls.append(args)
        return answers.get(args[0], OK)

    monkeypatch.setattr(manager, "_tmux_sync", tmux)

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    return manager, calls, answers


class TestLiteralSendArgv:
    async def test_leading_dash_text_survives_flag_parsing(self, fake_tmux) -> None:
        manager, calls, _ = fake_tmux
        assert await manager.send_keys("@1", "-1 за такой вариант") is True
        assert calls == [
            ("send-keys", "-t", "@1", "-l", "--", "-1 за такой вариант"),
            ("send-keys", "-t", "@1", "Enter"),  # an interpreted key, on its own
        ]

    async def test_literal_no_enter_uses_guarded_argv(self, fake_tmux) -> None:
        manager, calls, _ = fake_tmux
        assert await manager.send_keys("@1", "--help", enter=False) is True
        assert calls == [("send-keys", "-t", "@1", "-l", "--", "--help")]

    async def test_named_key_keeps_interpreted_path(self, fake_tmux) -> None:
        manager, calls, _ = fake_tmux
        ok = await manager.send_keys("@1", "Escape", enter=False, literal=False)
        assert ok is True
        assert calls == [("send-keys", "-t", "@1", "Escape")]

    async def test_long_text_chunked_each_chunk_guarded(self, fake_tmux) -> None:
        manager, calls, _ = fake_tmux
        assert await manager.send_keys("@1", "a" * 450) is True  # 200 + 200 + 50
        assert [c[-1] for c in calls[:-1]] == ["a" * 200, "a" * 200, "a" * 50]
        for call in calls[:-1]:
            assert call[:5] == ("send-keys", "-t", "@1", "-l", "--")
        assert calls[-1] == ("send-keys", "-t", "@1", "Enter")

    async def test_send_keys_failure_is_reported(self, fake_tmux) -> None:
        manager, calls, answers = fake_tmux
        answers["send-keys"] = TmuxResult(1, "", "can't find window: @1")
        assert await manager.send_keys("@1", "text") is False
        assert len(calls) == 1  # no Enter after a failed text


class TestSessionById:
    """ensure_session must publish KIKUBOT_DIR into the tmux session env.

    The SessionStart hook runs in agent panes and inherits the tmux server
    environment, not the bot's — a `.env`-only KIKUBOT_DIR would silently split
    session_map.json across two directories without this export.
    """

    def test_existing_session_gets_env_by_id(self, fake_tmux) -> None:
        manager, calls, answers = fake_tmux
        # "test-x" would match a `-t test` by prefix; ours is found exactly.
        answers["list-sessions"] = _sessions(("$7", "test-x"), ("$3", "test"))
        assert manager.ensure_session() == "$3"
        assert (
            "set-environment",
            "-t",
            "$3",
            "KIKUBOT_DIR",
            str(config.config_dir),
        ) in (calls)
        for var in config.sensitive_env_vars:
            assert ("set-environment", "-t", "$3", "-u", var) in calls
        assert not any(c[0] == "new-session" for c in calls)

    def test_missing_session_is_created(self, monkeypatch) -> None:
        manager = TmuxManager(session_name="test")
        sessions = [("$7", "test-x")]
        calls: list[tuple[str, ...]] = []

        def tmux(*args: str) -> TmuxResult:
            calls.append(args)
            if args[0] == "list-sessions":
                return _sessions(*sessions)
            if args[0] == "new-session":
                sessions.append(("$8", "test"))
            return OK

        monkeypatch.setattr(manager, "_tmux_sync", tmux)
        assert manager.ensure_session() == "$8"
        new = next(c for c in calls if c[0] == "new-session")
        assert new[:4] == ("new-session", "-d", "-s", "test")


@pytest.mark.skipif(TMUX is None, reason="tmux not installed")
class TestOnRealTmux:
    async def test_create_rename_capture_kill(self, tmux_server, tmp_path) -> None:
        tmux, manager, signal = tmux_server
        ok, _, name, wid = await manager.create_window(
            str(tmp_path), window_name="proj", start_claude=False
        )
        assert ok and name == "proj" and wid.startswith("@")
        # Same name again → deduplicated, never reused.
        ok, _, name2, wid2 = await manager.create_window(
            str(tmp_path), window_name="proj", start_claude=False
        )
        assert ok and name2 == "proj-2" and wid2 != wid
        assert tmux("show-options", "-w", "-t", wid, "-v", "allow-rename") == "off\n"

        assert await manager.send_keys(wid, f"{signal} up", enter=False)
        assert await manager.send_keys(wid, "", enter=True, literal=False)
        tmux("wait-for", "up")  # the shell runs what we typed
        # A leading "-" must arrive as text, not be parsed as a flag.
        assert await manager.send_keys(wid, "-n typed", enter=False)
        captured = ""
        for _ in range(100):
            captured = await manager.capture_pane(wid) or ""
            if "-n typed" in captured:
                break
            await asyncio.sleep(0.05)
        assert "-n typed" in captured
        assert not captured.endswith("\n")  # trailing blank rows dropped

        assert await manager.rename_window(wid, "renamed")
        window = await manager.find_window_by_id(wid)
        assert window is not None and window.window_name == "renamed"
        assert await manager.kill_window(wid)
        assert await manager.find_window_by_id(wid) is None
        assert await manager.kill_window(wid) is False  # already gone

    async def test_non_ascii_name_survives_a_bare_locale(
        self, tmux_server, tmp_path, monkeypatch
    ) -> None:
        # Started outside tmux (a service unit, cron) with no UTF-8 locale,
        # tmux prints «·» and Cyrillic as "_": the window would never match its
        # own name, so dedup and lookup by name would both go blind. ($TMUX
        # alone makes a client UTF-8, so it goes too.)
        for var in ("LANG", "LC_ALL", "LC_CTYPE", "TMUX"):
            monkeypatch.delenv(var, raising=False)
        _, manager, _ = tmux_server
        name = "app · дизайн"
        ok, _, created, wid = await manager.create_window(
            str(tmp_path), window_name=name, start_claude=False
        )
        assert ok and created == name
        found = await manager.find_window_by_name(name)
        assert found is not None and found.window_id == wid
        ok, _, second, _ = await manager.create_window(
            str(tmp_path), window_name=name, start_claude=False
        )
        assert ok and second == f"{name}-2"

    async def test_unique_name_ignores_the_window_itself(
        self, tmux_server, tmp_path
    ) -> None:
        _, manager, _ = tmux_server
        _, _, _, wid = await manager.create_window(
            str(tmp_path), window_name="app", start_claude=False
        )
        assert await manager.unique_window_name("app", own_id=wid) == "app"
        assert await manager.unique_window_name("app") == "app-2"

    async def test_ensure_session_creates_and_exports(self, tmux_server) -> None:
        tmux, manager, _ = tmux_server
        manager.session_name = "fresh"
        sid = manager.ensure_session()
        assert sid.startswith("$")
        assert tmux("list-windows", "-t", sid, "-F", "#{window_name}") == (
            f"{config.tmux_main_window_name}\n"
        )
        env = tmux("show-environment", "-t", sid, "KIKUBOT_DIR")
        assert env == f"KIKUBOT_DIR={config.config_dir}\n"
        assert manager.ensure_session() == sid  # idempotent

    async def test_pane_size_is_pinned(self, tmux_server) -> None:
        tmux, manager, _ = tmux_server
        await manager.ensure_session_pane_size(120, 40)
        assert tmux("display", "-p", "-t", "t:agent", "#{window_width}") == "120\n"

    async def test_capture_screen_with_history(self, tmux_server, tmp_path) -> None:
        tmux, manager, signal = tmux_server
        ok, _, _, wid = await manager.create_window(
            str(tmp_path), window_name="scr", start_claude=False
        )
        assert ok
        assert await manager.send_keys(wid, f"seq 1 300; {signal} done", enter=False)
        assert await manager.send_keys(wid, "", enter=True, literal=False)
        tmux("wait-for", "done")
        rows = int(tmux("display", "-p", "-t", wid, "#{pane_height}"))
        cols = int(tmux("display", "-p", "-t", wid, "#{pane_width}"))

        screen = await manager.capture_screen(wid)
        assert screen is not None
        assert (screen.cols, screen.rows, screen.back) == (cols, rows, 0)
        assert screen.history > 0 and screen.cursor is not None
        assert screen.text.count("\n") == rows and screen.text.endswith("\n")

        deep = await manager.capture_screen(wid, back=40)
        assert deep is not None and deep.back == 40
        lines = deep.text.split("\n")[:-1]
        assert len(lines) == 40 + rows
        assert lines[40:] == screen.text.split("\n")[:-1]
        assert await manager.capture_screen("@99999") is None


def test_launched_within_counts_from_the_launch(monkeypatch):
    """A just-launched agent's shell still holds the terminal: the dead-agent
    check must not judge that window yet."""
    mgr = TmuxManager(session_name="t", socket_name="unused")
    now = [1000.0]
    monkeypatch.setattr(tm_mod.time, "monotonic", lambda: now[0])
    assert not mgr.launched_within("@1", 15)
    mgr._launched_at["@1"] = now[0]
    now[0] += 5
    assert mgr.launched_within("@1", 15)
    now[0] += 11
    assert not mgr.launched_within("@1", 15)
    assert "@1" not in mgr._launched_at  # forgotten once past
