"""Tests for the `kikubot statusline` tap: what it records, and that the
user's own status line always runs unchanged after it."""

from __future__ import annotations

import io
import json
import os
import stat

from kikubot import statusline

_STDIN = {
    "session_id": "c1213b97-b5fd-490a-89db-7444cf40f95f",
    "transcript_path": "/home/user/.claude/projects/x/c1.jsonl",
    "cwd": "/home/user/project",
    "workspace": {
        "current_dir": "/home/user/project",
        "project_dir": "/home/user/project",
    },
    "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
    "effort": {"level": "high"},
    "thinking": {"enabled": True},
    "fast_mode": False,
    "version": "2.1.322",
    "output_style": {"name": "default"},
    "cost": {
        "total_cost_usd": 1.5,
        "total_duration_ms": 60000,
        "total_lines_added": 12,
        "total_lines_removed": 3,
    },
    "context_window": {
        "total_input_tokens": 5000,
        "context_window_size": 1000000,
        "used_percentage": 43,
    },
    "rate_limits": {
        "five_hour": {"used_percentage": 62, "resets_at": 1900000000},
        "seven_day": {"used_percentage": 20, "resets_at": 1900500000},
    },
}


def test_snapshot_keeps_only_non_private_fields():
    snap = statusline._snapshot(_STDIN)
    text = json.dumps(snap)
    assert "/home/user" not in text  # no paths, cwd or transcript
    assert snap["model"] == {"id": "claude-opus-5-5", "display_name": "Opus 5.5"}
    assert snap["context_window"]["used_percentage"] == 43
    assert snap["rate_limits"]["five_hour"] == {
        "used_percentage": 62,
        "resets_at": 1900000000,
    }
    assert snap["effort"] == "high"
    assert snap["thinking"] is True
    assert snap["cost"]["total_lines_added"] == 12
    assert (snap["version"], snap["output_style"]) == ("2.1.322", "default")


def test_snapshot_written_for_own_session_window(tmp_path, monkeypatch):
    monkeypatch.setattr(statusline, "_pane_window", lambda session: "@7")
    statusline.write_snapshot(json.dumps(_STDIN).encode(), tmp_path, "kikubot")
    target = tmp_path / "@7.json"
    assert json.loads(target.read_text())["session_id"] == _STDIN["session_id"]
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["@7.json"]  # no tmp left


def test_no_snapshot_outside_kikubot_session(tmp_path, monkeypatch):
    monkeypatch.setattr(statusline, "_pane_window", lambda session: None)
    statusline.write_snapshot(json.dumps(_STDIN).encode(), tmp_path, "kikubot")
    assert list(tmp_path.iterdir()) == []


def _settings(path, command):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"statusLine": {"type": "command", "command": command}}))


def test_user_command_follows_claude_precedence(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    raw = json.dumps({"workspace": {"project_dir": str(project)}}).encode()

    _settings(home / "settings.json", "user-line")
    assert statusline.user_statusline_command(raw) == "user-line"
    _settings(project / ".claude" / "settings.json", "project-line")
    assert statusline.user_statusline_command(raw) == "project-line"
    _settings(project / ".claude" / "settings.local.json", "local-line")
    assert statusline.user_statusline_command(raw) == "local-line"


def test_own_command_is_never_chained(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _settings(tmp_path / "settings.json", "/venv/bin/kikubot statusline /d kikubot")
    assert statusline.user_statusline_command(b"{}") is None


def _run_main(monkeypatch, argv, raw):
    class _Stdin:
        buffer = io.BytesIO(raw)

    monkeypatch.setattr(statusline.sys, "stdin", _Stdin())
    return statusline.statusline_main(argv)


def test_chain_passes_stdin_stdout_and_exit_code(tmp_path, monkeypatch, capfd):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _settings(tmp_path / "settings.json", "cat; printf ' line'; exit 3")
    rc = _run_main(monkeypatch, [], b'{"x":1}')
    assert rc == 3
    assert capfd.readouterr().out == '{"x":1} line'


def test_snapshot_failure_never_breaks_the_users_line(tmp_path, monkeypatch, capfd):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _settings(tmp_path / "settings.json", "printf ok")
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(statusline, "_pane_window", lambda session: "@1")
    rc = _run_main(monkeypatch, [str(blocker), "kikubot"], json.dumps(_STDIN).encode())
    assert rc == 0
    assert capfd.readouterr().out == "ok"


def test_no_user_command_prints_nothing(tmp_path, monkeypatch, capfd):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert _run_main(monkeypatch, [], b"{}") == 0
    assert capfd.readouterr().out == ""
