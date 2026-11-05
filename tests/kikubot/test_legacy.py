"""Tests for the move from ccbot to kikubot (legacy.py) — delete with it.

migrate() runs on every start of an install from before the rename: it must
move what is there once, leave a migrated install alone, and refuse — never
guess — when old and new both exist. The tmux half runs against a fake here
and once against a throwaway tmux server.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kikubot import legacy

SEP, END = "␟", "␞"
TMUX = shutil.which("tmux")


@pytest.fixture
def install(tmp_path, monkeypatch):
    """An install from before the rename: ~/.ccbot with state, a repo checkout."""
    for name in (
        "KIKUBOT_DIR",
        "CCBOT_DIR",
        "TMUX_SESSION_NAME",
        "CLAUDE_CONFIG_DIR",
        "KIKUBOT_CLAUDE_PROJECTS_PATH",
        "CCBOT_CLAUDE_PROJECTS_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    home, repo = tmp_path / "home", tmp_path / "repo"
    old = home / ".ccbot"
    (old / "logs").mkdir(parents=True)
    (old / "state.json").write_text("{}")
    (old / "logs" / "ccbot.log").write_text("old log\n")
    (old / "logs" / "ccbot.log.1").write_text("older log\n")
    repo.mkdir()
    return home, repo


@pytest.fixture
def tmux(monkeypatch):
    """Scripted tmux for migrate(): ``sessions`` maps name -> $id."""
    state = {"sessions": {}, "calls": []}

    def fake(*args: str) -> subprocess.CompletedProcess[str]:
        state["calls"].append(args)
        if args[0] == "list-sessions":
            out = "".join(
                f"{sid}{SEP}{name}{END}\n" for name, sid in state["sessions"].items()
            )
            return subprocess.CompletedProcess(args, 0, out, "")
        if args[0] == "rename-session":
            sid, new = args[2], args[3]
            name = next(n for n, s in state["sessions"].items() if s == sid)
            state["sessions"][new] = state["sessions"].pop(name)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(legacy, "_tmux", fake)
    return state


class TestDataDir:
    def test_moves_once_and_leaves_a_symlink(self, install, tmux) -> None:
        home, repo = install
        done = legacy.migrate(home, repo)
        new = home / ".kikubot"
        assert (new / "state.json").read_text() == "{}"
        assert (home / ".ccbot").is_symlink()
        assert (home / ".ccbot").resolve() == new.resolve()
        assert any("moved" in step for step in done)
        assert legacy.migrate(home, repo) == []  # a migrated install: nothing

    def test_refuses_when_both_dirs_exist(self, install, tmux) -> None:
        home, repo = install
        (home / ".kikubot").mkdir()
        with pytest.raises(legacy.MigrationError):
            legacy.migrate(home, repo)
        assert (home / ".ccbot" / "state.json").exists()  # nothing moved

    def test_an_explicit_dir_stays_put(self, install, tmux, monkeypatch) -> None:
        home, repo = install
        custom = home / "custom"
        custom.mkdir()
        monkeypatch.setenv("CCBOT_DIR", str(custom))
        legacy.migrate(home, repo)
        assert not (home / ".kikubot").exists()
        assert not (home / ".ccbot").is_symlink()

    def test_logs_are_renamed(self, install, tmux) -> None:
        home, repo = install
        legacy.migrate(home, repo)
        logs = home / ".kikubot" / "logs"
        assert sorted(p.name for p in logs.iterdir()) == [
            "kikubot.log",
            "kikubot.log.1",
        ]
        assert (logs / "kikubot.log").read_text() == "old log\n"


class TestWorktreePaths:
    """Agent worktrees live in the data dir: moving it moved them."""

    def test_state_paths_follow_the_dir(self, install, tmux) -> None:
        home, repo = install
        old, new = home / ".ccbot", home / ".kikubot"
        wt = f"{old}/worktrees/app/fix"
        (old / "state.json").write_text(
            json.dumps(
                {
                    "thread_directory_memory": {"1": {"75": wt, "76": "/srv/app"}},
                    "worktree_meta": {"1": {"75": {"path": wt, "repo": "/srv/app"}}},
                    "names": [f"{old}x/not-ours", "ccbot"],
                }
            )
        )
        (old / "session_map.json").write_text(json.dumps({"other:@1": {"cwd": wt}}))
        legacy.migrate(home, repo)
        state = json.loads((new / "state.json").read_text())
        moved = f"{new}/worktrees/app/fix"
        assert state["thread_directory_memory"] == {
            "1": {"75": moved, "76": "/srv/app"}
        }
        assert state["worktree_meta"]["1"]["75"] == {"path": moved, "repo": "/srv/app"}
        assert state["names"] == [f"{old}x/not-ours", "ccbot"]
        entries = json.loads((new / "session_map.json").read_text())
        assert entries == {"other:@1": {"cwd": moved}}

    def test_an_install_moved_earlier_is_finished_on_the_next_start(
        self, install, tmux
    ) -> None:
        home, repo = install
        legacy.migrate(home, repo)  # moved by a version without this step
        wt = f"{home / '.ccbot'}/worktrees/app/fix"
        (home / ".kikubot" / "state.json").write_text(json.dumps({"dir": wt}))
        legacy.migrate(home, repo)
        state = json.loads((home / ".kikubot" / "state.json").read_text())
        assert state == {"dir": f"{home / '.kikubot'}/worktrees/app/fix"}

    def test_transcripts_move_to_the_name_of_the_new_path(self, install, tmux) -> None:
        home, repo = install
        projects = home / ".claude" / "projects"
        name = legacy._project_dir_name
        real_home = os.path.realpath(home)
        old = projects / name(f"{real_home}/.ccbot/worktrees/app/fix")
        other = projects / name(f"{real_home}/dev/app")
        old.mkdir(parents=True)
        other.mkdir()
        (old / "sid.jsonl").write_text("{}\n")
        legacy.migrate(home, repo)
        new = projects / name(f"{real_home}/.kikubot/worktrees/app/fix")
        assert (new / "sid.jsonl").read_text() == "{}\n"
        assert not new.is_symlink()
        assert old.is_symlink() and old.resolve() == new.resolve()
        assert other.is_dir() and not other.is_symlink()
        assert legacy.migrate(home, repo) == []  # done once

    def test_transcripts_under_both_names_are_left_alone(self, install, tmux) -> None:
        home, repo = install
        projects = home / ".claude" / "projects"
        real_home = os.path.realpath(home)
        old = projects / legacy._project_dir_name(f"{real_home}/.ccbot/worktrees/a/b")
        new = projects / legacy._project_dir_name(f"{real_home}/.kikubot/worktrees/a/b")
        old.mkdir(parents=True)
        new.mkdir()
        done = legacy.migrate(home, repo)
        assert old.is_dir() and not old.is_symlink()
        assert any("both names" in step for step in done)

    @pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
    def test_git_registers_the_worktree_at_its_new_path(self, install, tmux) -> None:
        home, repo = install

        def git(*args: str, cwd: Path = repo) -> str:
            return subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=t",
                    "-c",
                    "user.email=t@t",
                    "-c",
                    "core.hooksPath=/dev/null",
                    *args,
                ],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            ).stdout

        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "init")
        old_wt = home / ".ccbot" / "worktrees" / "app" / "fix"
        git("worktree", "add", "-q", "-b", "wt/fix", str(old_wt))
        legacy.migrate(home, repo)
        new_wt = Path(os.path.realpath(home / ".kikubot" / "worktrees" / "app" / "fix"))
        listed = git("worktree", "list", "--porcelain")
        assert f"worktree {new_wt}\n" in listed
        assert ".ccbot" not in listed
        assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=new_wt) == "wt/fix\n"


class TestEnvFiles:
    def test_old_names_renamed_values_and_mode_kept(self, install, tmux) -> None:
        home, repo = install
        env = repo / ".env"
        env.write_text(
            "TELEGRAM_BOT_TOKEN=x\n"
            "CCBOT_PLUGINS=drive\n"
            "  export CCBOT_DIR_HINT=CCBOT_value\n"
            "# CCBOT_COMMENTED=1\n"
        )
        os.chmod(env, 0o600)
        legacy.migrate(home, repo)
        assert env.read_text() == (
            "TELEGRAM_BOT_TOKEN=x\n"
            "KIKUBOT_PLUGINS=drive\n"
            "  export KIKUBOT_DIR_HINT=CCBOT_value\n"  # a value is never touched
            "# CCBOT_COMMENTED=1\n"
        )
        assert env.stat().st_mode & 0o777 == 0o600

    def test_data_dir_env_too(self, install, tmux) -> None:
        home, repo = install
        (home / ".ccbot" / ".env").write_text("CCBOT_MAIL_ENABLED=true\n")
        legacy.migrate(home, repo)
        assert (home / ".kikubot" / ".env").read_text() == "KIKUBOT_MAIL_ENABLED=true\n"


class TestTmuxSession:
    def _session_map(self, home: Path, keys: dict) -> Path:
        path = home / ".ccbot" / "session_map.json"
        path.write_text(json.dumps(keys))
        return path

    def test_renamed_live_with_its_session_map_keys(self, install, tmux) -> None:
        home, repo = install
        tmux["sessions"] = {"ccbot": "$3", "other": "$1"}
        self._session_map(home, {"ccbot:@4": {"session_id": "a"}, "other:@1": {}})
        legacy.migrate(home, repo)
        assert tmux["sessions"] == {"kikubot": "$3", "other": "$1"}
        assert ("rename-session", "-t", "$3", "kikubot") in tmux["calls"]
        entries = json.loads((home / ".kikubot" / "session_map.json").read_text())
        assert entries == {"kikubot:@4": {"session_id": "a"}, "other:@1": {}}

    def test_already_renamed_session_loses_the_old_dir_variable(
        self, install, tmux
    ) -> None:
        home, repo = install
        tmux["sessions"] = {"kikubot": "$3"}  # scripts/restart.sh renamed it first
        self._session_map(home, {"ccbot:@4": {}})
        legacy.migrate(home, repo)
        assert ("set-environment", "-t", "$3", "-u", "CCBOT_DIR") in tmux["calls"]
        assert not any(c[0] == "rename-session" for c in tmux["calls"])
        entries = json.loads((home / ".kikubot" / "session_map.json").read_text())
        assert entries == {"kikubot:@4": {}}

    def test_a_configured_name_is_left_alone(self, install, tmux, monkeypatch) -> None:
        home, repo = install
        (repo / ".env").write_text("TMUX_SESSION_NAME=ccbot\n")
        tmux["sessions"] = {"ccbot": "$3"}
        self._session_map(home, {"ccbot:@4": {}})
        legacy.migrate(home, repo)
        assert tmux["sessions"] == {"ccbot": "$3"}
        entries = json.loads((home / ".kikubot" / "session_map.json").read_text())
        assert entries == {"ccbot:@4": {}}

    def test_refuses_when_both_sessions_exist(self, install, tmux) -> None:
        home, repo = install
        tmux["sessions"] = {"ccbot": "$3", "kikubot": "$5"}
        with pytest.raises(legacy.MigrationError):
            legacy.migrate(home, repo)

    def test_no_tmux_server_is_fine(self, install, monkeypatch) -> None:
        home, repo = install
        monkeypatch.setattr(
            legacy,
            "_tmux",
            lambda *a: subprocess.CompletedProcess(a, 1, "", "no server running on x"),
        )
        legacy.migrate(home, repo)  # nothing to rename, no error


class TestEnvironment:
    def test_adopt_env(self) -> None:
        env = {"CCBOT_PLUGINS": "drive", "CCBOT_DIR": "/old", "KIKUBOT_DIR": "/new"}
        assert legacy.adopt_env(env) == ["CCBOT_PLUGINS"]
        # The new name wins; the old ones leave either way (no leak to agents).
        assert env == {"KIKUBOT_PLUGINS": "drive", "KIKUBOT_DIR": "/new"}

    def test_old_hook_command(self) -> None:
        assert legacy.is_hook_command("/v/bin/ccbot hook")
        assert legacy.is_hook_command("'/a b/bin/ccbot' hook")
        assert not legacy.is_hook_command("/v/bin/kikubot hook")
        assert not legacy.is_hook_command("/v/bin/ccbot")


@pytest.mark.skipif(TMUX is None, reason="tmux not installed")
class TestOnRealTmux:
    def test_session_renamed_and_its_windows_survive(
        self, install, monkeypatch
    ) -> None:
        home, repo = install
        socket = f"kikubot-legacy-{os.getpid()}"
        monkeypatch.setattr(legacy, "TMUX_SOCKET", socket)
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}

        def tmux(*args: str) -> str:
            return subprocess.run(
                [TMUX, "-L", socket, "-f", "/dev/null", *args],  # type: ignore[list-item]
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout

        tmux("new-session", "-d", "-s", "ccbot", "-n", "agent", "sleep 300")
        try:
            before = tmux("list-windows", "-a", "-F", "#{window_id} #{pane_pid}")
            legacy.migrate(home, repo)
            after = tmux("list-windows", "-a", "-F", "#{window_id} #{pane_pid}")
            assert tmux("list-sessions", "-F", "#{session_name}") == "kikubot\n"
            assert after == before  # same window, same running process
        finally:
            subprocess.run(
                [TMUX, "-L", socket, "kill-server"],  # type: ignore[list-item]
                env=env,
                capture_output=True,
                timeout=10,
            )
